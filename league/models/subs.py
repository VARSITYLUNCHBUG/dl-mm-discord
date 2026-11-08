"""Player-driven substitutes. Subbing out opens a slot; subbing in fills it. They are separate rows
because a slot can be offered to several people in turn before someone takes it."""

from __future__ import annotations

from django.db import models
from django.db.models import Q


class SubStatus(models.TextChoices):
    AWAITING_APPROVAL = "approval", "Waiting for the other captain"
    OPEN = "open", "Looking for a sub"
    FILLED = "filled", "Filled"
    CANCELLED = "cancelled", "Cancelled"
    REJECTED = "rejected", "Not approved"
    EXPIRED = "expired", "Nobody filled it in time"


UNRESOLVED = (SubStatus.AWAITING_APPROVAL, SubStatus.OPEN)


class SubRequest(models.Model):
    """A request to take one player out of a live match.

    A player can ask for themself, which opens the slot straight away. A captain can ask for a
    teammate, which needs the other captain to agree first. Until the request is settled the
    match is on hold: its timer is parked on the match and the request's own deadline takes over.
    """

    match = models.ForeignKey("league.Match", on_delete=models.PROTECT, related_name="sub_requests")
    leaving = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    requested_by = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=10, choices=SubStatus.choices)
    # Approval deadline while waiting on the other captain; the match-cancel deadline once open.
    deadline_at = models.DateTimeField()
    # Until then, whoever opened the slot can name a sub before the queue is asked.
    name_until = models.DateTimeField(null=True, blank=True)
    filled_by = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField()
    opened_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sub_requests"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("match", "leaving"),
                condition=Q(status__in=["approval", "open"]),
                name="uniq_unresolved_sub_per_player",
            ),
        ]
        indexes = [
            models.Index(
                fields=("deadline_at",),
                condition=Q(status__in=["approval", "open"]),
                name="sub_unresolved_deadline_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Sub out {self.leaving_id} from {self.match_id} ({self.status})"

    @property
    def is_self(self) -> bool:
        return self.leaving_id == self.requested_by_id


class OfferSource(models.TextChoices):
    INVITE = "invite", "Invited by name"
    QUEUE = "queue", "Next in the queue"


class OfferStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    EXPIRED = "expired", "Expired"
    FAILED = "failed", "Couldn't reach them"
    WITHDRAWN = "withdrawn", "Withdrawn"


class SubOffer(models.Model):
    """One person asked to take an open slot. At most one is pending per slot at a time."""

    request = models.ForeignKey(SubRequest, on_delete=models.PROTECT, related_name="offers")
    player = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=8, choices=OfferSource.choices)
    invited_by = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    status = models.CharField(
        max_length=10, choices=OfferStatus.choices, default=OfferStatus.PENDING
    )
    deadline_at = models.DateTimeField()
    created_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "sub_offers"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("request",),
                condition=Q(status="pending"),
                name="uniq_pending_offer_per_sub",
            ),
            # Nobody is asked twice for the same slot, so a decline sticks.
            models.UniqueConstraint(fields=("request", "player"), name="uniq_offer_per_player"),
        ]
        indexes = [
            models.Index(
                fields=("deadline_at",),
                condition=Q(status="pending"),
                name="sub_offer_pending_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Offer {self.request_id} to {self.player_id} ({self.status})"
