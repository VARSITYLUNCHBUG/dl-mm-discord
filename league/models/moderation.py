from __future__ import annotations

from django.db import models


class PenaltyKind(models.TextChoices):
    MISSED_READY = "missed_ready", "Missed ready check"
    DODGE = "dodge", "Dodge"
    LEAVER = "leaver", "Leaver"
    FALSE_REPORT = "false_report", "False report"
    MANUAL = "manual", "Manual"


class Penalty(models.Model):
    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    player = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="penalties")
    match = models.ForeignKey(
        "league.Match", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=16, choices=PenaltyKind.choices)
    tier = models.PositiveSmallIntegerField(default=1)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    reason = models.CharField(max_length=500, blank=True)
    issued_by = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "penalties"
        ordering = ("-starts_at",)
        indexes = [models.Index(fields=("player", "expires_at"), name="penalty_active_idx")]

    def __str__(self) -> str:
        return f"{self.player_id}: {self.kind} until {self.expires_at:%Y-%m-%d %H:%M}"
