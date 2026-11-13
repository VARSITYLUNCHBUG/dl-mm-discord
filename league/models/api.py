"""What deadlock-api said about a match, checked against what the captains reported."""

from __future__ import annotations

from django.db import models

from league.models.matches import Team


class CheckStatus(models.TextChoices):
    OK = "ok", "Agrees with the result"
    MISMATCH = "mismatch", "Disagrees with the result"
    NOT_FOUND = "not_found", "Not in deadlock-api yet"
    ERROR = "error", "The request failed"


class ApiMatchCheck(models.Model):
    """One fetch of a match's metadata. Append-only: each retry is a new row."""

    match = models.ForeignKey("league.Match", on_delete=models.PROTECT, related_name="api_checks")
    deadlock_match_id = models.BigIntegerField()
    attempt = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=12, choices=CheckStatus.choices)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    api_winner = models.CharField(max_length=1, choices=Team.choices, blank=True)
    duration_s = models.PositiveIntegerField(null=True, blank=True)
    # Linked players the API showed on the other team, or not in the match at all.
    wrong_team = models.JSONField(default=list, blank=True)
    missing = models.JSONField(default=list, blank=True)
    # Accounts in the match that aren't linked to anyone.
    unlinked_accounts = models.JSONField(default=list, blank=True)
    # A trimmed copy of the metadata: per player account, team, hero, K/D/A, mvp_rank.
    summary = models.JSONField(default=dict, blank=True)
    problems = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "api_match_checks"
        ordering = ("id",)
        indexes = [models.Index(fields=("match", "-id"), name="api_check_match_idx")]

    def __str__(self) -> str:
        return f"{self.match_id} #{self.attempt}: {self.status}"
