from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models import Q


class QuickDraftStatus(models.TextChoices):
    CAPTAINS = "captains", "Waiting for captains"
    COINFLIP = "coinflip", "Captain coinflip"
    READY = "ready", "Ready to start"
    DRAFTING = "drafting", "Drafting"
    COMPLETED = "completed", "Completed"
    STOPPED = "stopped", "Stopped"


QUICK_DRAFT_LIVE = (
    QuickDraftStatus.CAPTAINS,
    QuickDraftStatus.COINFLIP,
    QuickDraftStatus.READY,
    QuickDraftStatus.DRAFTING,
)


class Side(models.TextChoices):
    HIDDEN_KING = "hk", "The Hidden King"
    ARCHMOTHER = "am", "The Archmother"


class QuickDraft(models.Model):
    """Bebot's /startdraft: a hero ban and pick in any channel, outside the match flow.

    Two captains, one draft message with the hero grid below it. Nothing here touches ratings.
    Captains are plain Discord users, so a draft can run for people who have never queued.
    """

    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    mode_version = models.ForeignKey(
        "league.DraftModeVersion", on_delete=models.PROTECT, related_name="+"
    )
    channel_id = models.BigIntegerField()
    message_id = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=QuickDraftStatus.choices)
    started_by_id = models.BigIntegerField()
    hk_captain_id = models.BigIntegerField(null=True, blank=True)
    hk_captain_name = models.CharField(max_length=100, blank=True)
    am_captain_id = models.BigIntegerField(null=True, blank=True)
    am_captain_name = models.CharField(max_length=100, blank=True)
    # The side in the mode's "first" role: it bans and picks first.
    first_side = models.CharField(max_length=2, choices=Side.choices, default=Side.HIDDEN_KING)
    # /captains_coinflip: the winner chooses first, then the loser picks from what's left.
    coinflip_winner_id = models.BigIntegerField(null=True, blank=True)
    coinflip_winner_name = models.CharField(max_length=100, blank=True)
    coinflip_loser_id = models.BigIntegerField(null=True, blank=True)
    coinflip_loser_name = models.CharField(max_length=100, blank=True)
    winner_choice = models.CharField(max_length=12, blank=True)
    loser_choice = models.CharField(max_length=12, blank=True)
    step_index = models.PositiveSmallIntegerField(default=0)
    # The hero grid's messages below the draft message, top to bottom, as in the match flow.
    grid_message_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    version = models.PositiveIntegerField(default=0)
    deadline_at = models.DateTimeField(null=True, blank=True)
    rng_seed = models.BigIntegerField()
    champion_pool = ArrayField(models.PositiveIntegerField(), default=list)
    created_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "quick_drafts"
        ordering = ("-id",)
        constraints = [
            models.UniqueConstraint(
                fields=("channel_id",),
                condition=Q(status__in=("captains", "coinflip", "ready", "drafting")),
                name="uniq_live_quick_draft_per_channel",
            ),
        ]
        indexes = [
            models.Index(
                fields=("deadline_at",),
                condition=Q(deadline_at__isnull=False),
                name="quick_draft_deadline_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Draft #{self.pk} ({self.status})"

    def captain_id(self, side: str) -> int | None:
        return self.hk_captain_id if side == Side.HIDDEN_KING else self.am_captain_id

    def captain_name(self, side: str) -> str:
        return self.hk_captain_name if side == Side.HIDDEN_KING else self.am_captain_name


class QuickDraftAction(models.Model):
    """One ban or pick. The unique step stops a double click taking two turns."""

    draft = models.ForeignKey(QuickDraft, on_delete=models.CASCADE, related_name="actions")
    step_index = models.PositiveSmallIntegerField()
    action = models.CharField(max_length=4)
    side = models.CharField(max_length=2, choices=Side.choices)
    champion = models.ForeignKey("league.Champion", on_delete=models.PROTECT, related_name="+")
    source = models.CharField(max_length=8)
    random_choice = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "quick_draft_actions"
        ordering = ("draft", "step_index")
        constraints = [
            models.UniqueConstraint(fields=("draft", "step_index"), name="uniq_quick_draft_step"),
            models.UniqueConstraint(fields=("draft", "champion"), name="uniq_quick_draft_champion"),
        ]

    def __str__(self) -> str:
        return f"{self.draft_id}#{self.step_index} {self.action}"
