"""What the score OCR read from report screenshots, and the awards that feed the rating."""

from __future__ import annotations

from django.db import models
from django.db.models import Q

from league.models.matches import Team


class ReadStatus(models.TextChoices):
    OK = "ok", "Read cleanly"
    NEEDS_MAPPING = "needs_mapping", "Some names need matching to players"
    NEEDS_REVIEW = "needs_review", "Read, with problems to check"
    REJECTED = "rejected", "Not a scoreboard"
    FAILED = "failed", "The reader failed"


class ScoreboardRead(models.Model):
    """One OCR read of one report screenshot. Append-only: a re-read is a new row."""

    match = models.ForeignKey(
        "league.Match", on_delete=models.PROTECT, related_name="scoreboard_reads"
    )
    submission = models.ForeignKey(
        "league.ResultSubmission", on_delete=models.PROTECT, related_name="scoreboard_reads"
    )
    image_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=ReadStatus.choices)
    engine_version = models.CharField(max_length=32, blank=True)
    raw = models.JSONField(default=dict, blank=True)  # The engine's dict, as returned.
    problems = models.JSONField(default=list, blank=True)
    ocr_winner = models.CharField(max_length=1, choices=Team.choices, blank=True)
    ocr_match_id = models.BigIntegerField(null=True, blank=True)
    duration_s = models.PositiveIntegerField(null=True, blank=True)
    # Exactly one MVP and two key players were read, so the badges can be trusted.
    badges_clean = models.BooleanField(default=False)
    # One team, and only one, was read as the winner.
    winner_clean = models.BooleanField(default=False)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "scoreboard_reads"
        ordering = ("id",)
        indexes = [models.Index(fields=("match", "-id"), name="scoreboard_read_match_idx")]

    def __str__(self) -> str:
        return f"{self.match_id}: {self.status}"


class MapSource(models.TextChoices):
    AUTO = "auto", "Matched by name"
    ALIAS = "alias", "Matched by a confirmed in-game name"
    CAPTAIN = "captain", "Matched by a captain"
    STAFF = "staff", "Matched by staff"


class PlayerMatchStat(models.Model):
    """One scoreboard row. `player` stays empty until the name is matched to someone."""

    read = models.ForeignKey(ScoreboardRead, on_delete=models.PROTECT, related_name="stats")
    match = models.ForeignKey("league.Match", on_delete=models.PROTECT, related_name="+")
    row = models.PositiveSmallIntegerField()  # 0 to 11, in scoreboard order.
    player = models.ForeignKey(
        "league.Player",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="match_stats",
    )
    team = models.CharField(max_length=1, choices=Team.choices, blank=True)
    ocr_name = models.CharField(max_length=100, blank=True)
    hero = models.ForeignKey(
        "league.Champion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    hero_name = models.CharField(max_length=60, blank=True)
    badge = models.CharField(max_length=8, blank=True)  # "mvp", "key" or empty.
    souls = models.PositiveIntegerField(null=True, blank=True)
    kills = models.PositiveSmallIntegerField(null=True, blank=True)
    deaths = models.PositiveSmallIntegerField(null=True, blank=True)
    assists = models.PositiveSmallIntegerField(null=True, blank=True)
    player_damage = models.PositiveIntegerField(null=True, blank=True)
    objective_damage = models.PositiveIntegerField(null=True, blank=True)
    healing = models.PositiveIntegerField(null=True, blank=True)
    confidence = models.FloatField(default=0)
    map_score = models.FloatField(default=0)
    map_source = models.CharField(max_length=8, choices=MapSource.choices, blank=True)

    class Meta:
        db_table = "player_match_stats"
        ordering = ("read", "row")
        constraints = [
            models.UniqueConstraint(fields=("read", "row"), name="uniq_stat_row"),
            models.UniqueConstraint(
                fields=("read", "player"),
                condition=Q(player__isnull=False),
                name="uniq_stat_player",
            ),
        ]
        indexes = [models.Index(fields=("player", "-match"), name="stat_player_idx")]

    def __str__(self) -> str:
        return f"{self.ocr_name} ({self.match_id})"


class AwardKind(models.TextChoices):
    MVP = "mvp", "MVP"
    KEY = "key", "Key player"


class AwardSource(models.TextChoices):
    OCR = "ocr", "Scoreboard"
    REPORT = "report", "Captain's report"
    API = "api", "deadlock-api"
    STAFF = "staff", "Staff"


class MatchAward(models.Model):
    """The MVP and key players of a match. Append-only, in revisions: the rows sharing the
    highest revision are the current set, and a revision with no players clears them."""

    match = models.ForeignKey("league.Match", on_delete=models.PROTECT, related_name="awards")
    revision = models.PositiveIntegerField()
    player = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="awards"
    )
    kind = models.CharField(max_length=4, choices=AwardKind.choices, blank=True)
    source = models.CharField(max_length=8, choices=AwardSource.choices)
    actor = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField()

    class Meta:
        db_table = "match_awards"
        ordering = ("match", "revision", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("match", "revision", "player"),
                condition=Q(player__isnull=False),
                name="uniq_award_revision_player",
            ),
        ]
        indexes = [models.Index(fields=("match", "-revision"), name="award_match_idx")]

    def __str__(self) -> str:
        return f"{self.match_id} rev {self.revision}: {self.kind} {self.player_id}"


class PlayerAlias(models.Model):
    """An in-game name seen on a scoreboard for a player. Confirmed ones map automatically."""

    player = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="aliases")
    name = models.CharField(max_length=100)
    name_norm = models.CharField(max_length=100)
    confirmed = models.BooleanField(default=False)
    times_seen = models.PositiveIntegerField(default=1)
    last_seen_at = models.DateTimeField()

    class Meta:
        db_table = "player_aliases"
        constraints = [
            models.UniqueConstraint(fields=("player", "name_norm"), name="uniq_player_alias"),
        ]
        indexes = [models.Index(fields=("name_norm",), name="alias_name_idx")]

    def __str__(self) -> str:
        return f"{self.name} -> {self.player_id}"
