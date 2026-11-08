from __future__ import annotations

from django.db import models

from league.models.matches import Source, Team


class Verdict(models.TextChoices):
    REPORT = "report", "Report"
    CONFIRM = "confirm", "Confirm"
    DISPUTE = "dispute", "Dispute"


class ResultSubmission(models.Model):
    """What each captain said about the result. The entry point for off-bot analytics.

    Screenshots live in the private archive channel. Keep message and attachment IDs, not URLs:
    Discord CDN URLs are signed and expire.
    """

    match = models.ForeignKey("league.Match", on_delete=models.PROTECT, related_name="submissions")
    captain = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="+")
    verdict = models.CharField(max_length=8, choices=Verdict.choices)
    claimed_winner = models.CharField(max_length=1, choices=Team.choices, blank=True)
    deadlock_match_id = models.BigIntegerField(null=True, blank=True)
    image_sha256 = models.CharField(max_length=64, blank=True)
    archive_channel_id = models.BigIntegerField(null=True, blank=True)
    archive_message_id = models.BigIntegerField(null=True, blank=True)
    attachment_id = models.BigIntegerField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "result_submissions"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=("match", "captain"), name="uniq_submission_captain"),
        ]

    def __str__(self) -> str:
        return f"{self.match_id}: {self.verdict} by {self.captain_id}"


class Outcome(models.TextChoices):
    A = "A", "Team A won"
    B = "B", "Team B won"
    VOID = "void", "Void"


class MatchResult(models.Model):
    """Append-only result facts. The highest revision is the current result."""

    match = models.ForeignKey("league.Match", on_delete=models.PROTECT, related_name="results")
    revision = models.PositiveIntegerField()
    outcome = models.CharField(max_length=4, choices=Outcome.choices)
    source = models.CharField(max_length=8, choices=Source.choices)
    actor = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "match_results"
        ordering = ("match", "revision")
        constraints = [
            models.UniqueConstraint(fields=("match", "revision"), name="uniq_result_revision"),
        ]

    def __str__(self) -> str:
        return f"{self.match_id} rev {self.revision}: {self.outcome}"
