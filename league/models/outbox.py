from __future__ import annotations

from django.db import models
from django.db.models import Q


class EffectStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"
    SKIPPED = "skipped", "Skipped"


class OutboxEffect(models.Model):
    """A Discord side effect, written in the same transaction as the state change it follows.

    The bot's worker runs these after commit, so a crash can never leave state and Discord
    disagreeing in a way a retry cannot fix. `run_after` also covers delayed work such as the
    game-end reminder and channel cleanup.
    """

    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    match = models.ForeignKey(
        "league.Match", null=True, blank=True, on_delete=models.PROTECT, related_name="effects"
    )
    kind = models.CharField(max_length=32)
    payload = models.JSONField(default=dict)
    dedupe_key = models.CharField(max_length=120, null=True, blank=True, unique=True)
    status = models.CharField(
        max_length=8, choices=EffectStatus.choices, default=EffectStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    run_after = models.DateTimeField()
    # Set when the worker begins a run, so a render requested after that gets one of its own.
    started_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField()
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "outbox_effects"
        ordering = ("id",)
        indexes = [
            models.Index(
                fields=("run_after", "id"),
                condition=Q(status="pending"),
                name="outbox_pending_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} ({self.status})"
