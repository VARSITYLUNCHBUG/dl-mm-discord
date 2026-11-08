from __future__ import annotations

from django.db import models
from django.db.models import Q


class Season(models.Model):
    """Rating parameters live here, so they are fixed for everything rated in the season."""

    community = models.ForeignKey(
        "league.Community", on_delete=models.PROTECT, related_name="seasons"
    )
    number = models.PositiveIntegerField()
    name = models.CharField(max_length=100)
    start_rating = models.IntegerField(default=1000)
    k_factor = models.PositiveSmallIntegerField(default=32)
    algo_version = models.CharField(max_length=16, default="elo_v1")
    leaderboard_min_games = models.PositiveSmallIntegerField(default=10)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "seasons"
        ordering = ("community", "-number")
        constraints = [
            models.UniqueConstraint(fields=("community", "number"), name="uniq_season_number"),
            models.UniqueConstraint(
                fields=("community",),
                condition=Q(ended_at__isnull=True),
                name="uniq_open_season",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class PlayerRating(models.Model):
    """The current Elo. A projection: `rebuild_ratings` can recreate it from the ledger."""

    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="ratings")
    player = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="ratings")
    rating = models.IntegerField()
    games = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    last_played_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "player_ratings"
        constraints = [
            models.UniqueConstraint(fields=("season", "player"), name="uniq_season_player"),
        ]
        indexes = [models.Index(fields=("season", "-rating"), name="leaderboard_idx")]

    def __str__(self) -> str:
        return f"{self.player_id}: {self.rating}"


class RunKind(models.TextChoices):
    LIVE = "live", "Live result"
    CORRECTION = "correction", "Correction"
    REPLAY = "replay", "Replay"
    SEED = "seed", "Seed"
    SEASON_RESET = "season_reset", "Season reset"
    ADMIN = "admin", "Admin adjustment"


class RatingRun(models.Model):
    """One applied computation. The unique key is what makes applying a result idempotent."""

    idempotency_key = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=12, choices=RunKind.choices)
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="+")
    match = models.ForeignKey(
        "league.Match", null=True, blank=True, on_delete=models.PROTECT, related_name="rating_runs"
    )
    algo_version = models.CharField(max_length=16)
    params = models.JSONField(default=dict)
    actor = models.ForeignKey(
        "league.Player", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "rating_runs"
        ordering = ("id",)

    def __str__(self) -> str:
        return self.idempotency_key


class RatingEvent(models.Model):
    """The append-only ledger. A player's Elo history is this table, in order."""

    run = models.ForeignKey(RatingRun, on_delete=models.PROTECT, related_name="events")
    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="+")
    player = models.ForeignKey(
        "league.Player", on_delete=models.PROTECT, related_name="rating_events"
    )
    match = models.ForeignKey(
        "league.Match",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rating_events",
    )
    delta = models.IntegerField()
    rating_after = models.IntegerField()
    expected = models.FloatField(null=True, blank=True)
    k = models.PositiveSmallIntegerField(null=True, blank=True)
    team_avg = models.FloatField(null=True, blank=True)
    opp_avg = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField()

    class Meta:
        db_table = "rating_events"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("run", "player", "match"), name="uniq_rating_event_run_player_match"
            ),
        ]
        indexes = [
            models.Index(fields=("player", "-id"), name="rating_history_idx"),
            models.Index(fields=("season", "match", "player"), name="rating_match_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.player_id} {self.delta:+d}"
