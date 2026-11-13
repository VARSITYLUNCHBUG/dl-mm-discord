from __future__ import annotations

from django.db import models
from django.db.models import Q


class PrioEntry(models.Model):
    """One member's place on the voice priority list (see `league.services.prio`).

    `queued_at` is when they joined the Waiting Room. Leaving sets `grace_until`; coming back
    before then clears it and keeps their place.
    """

    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    discord_user_id = models.BigIntegerField()
    display_name = models.CharField(max_length=100)
    queued_at = models.DateTimeField()
    grace_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "prio_entries"
        ordering = ("queued_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("community", "discord_user_id"), name="uniq_prio_member"
            ),
        ]
        indexes = [
            models.Index(
                fields=("grace_until",),
                condition=Q(grace_until__isnull=False),
                name="prio_grace_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.discord_user_id})"
