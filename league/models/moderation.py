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
    # Strikes this offence added, fixed when issued so a later policy edit can't rewrite history.
    strikes = models.PositiveSmallIntegerField(default=0)
    # Staff lifted it as a mistake, so its strikes don't count either.
    strikes_waived = models.BooleanField(default=False)

    class Meta:
        db_table = "penalties"
        ordering = ("-starts_at",)
        indexes = [models.Index(fields=("player", "expires_at"), name="penalty_active_idx")]

    def __str__(self) -> str:
        return f"{self.player_id}: {self.kind} until {self.expires_at:%Y-%m-%d %H:%M}"


class StrikeAdjustment(models.Model):
    """Staff adding or removing strikes by hand. Append-only: a change is a new row."""

    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    player = models.ForeignKey(
        "league.Player", on_delete=models.PROTECT, related_name="strike_adjustments"
    )
    delta = models.SmallIntegerField()
    actor = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=500)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "strike_adjustments"
        ordering = ("id",)
        indexes = [models.Index(fields=("community", "player"), name="strike_adjust_player_idx")]

    def __str__(self) -> str:
        return f"{self.player_id}: {self.delta:+d}"


class CaptainStatus(models.TextChoices):
    ALLOW = "allow", "Always allowed to captain"
    BAR = "bar", "Barred from captaining"
    CLEAR = "clear", "Back to the normal rule"


class CaptainOverride(models.Model):
    """Staff deciding who may captain, whatever their games. Append-only; the latest row wins."""

    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    player = models.ForeignKey(
        "league.Player", on_delete=models.PROTECT, related_name="captain_overrides"
    )
    status = models.CharField(max_length=8, choices=CaptainStatus.choices)
    actor = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "captain_overrides"
        ordering = ("id",)
        indexes = [models.Index(fields=("community", "player", "-id"), name="captain_override_idx")]

    def __str__(self) -> str:
        return f"{self.player_id}: {self.status}"
