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
    # OpenSkill parameters, over `openskill_v1.DEFAULT_PARAMS`. Fixed once the season rates.
    rating_params = models.JSONField(default=dict, blank=True)
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

    @property
    def start_rating(self) -> int:
        """The shown rating of a player who hasn't played: the projection's starting point."""
        from league.domain.openskill_v1 import OpenSkillAlgo, merged_params

        return OpenSkillAlgo().initial(merged_params(self.rating_params)).rating


class PlayerRating(models.Model):
    """The current rating. A projection: `rebuild_ratings` can recreate it from the ledger."""

    season = models.ForeignKey(Season, on_delete=models.PROTECT, related_name="ratings")
    player = models.ForeignKey("league.Player", on_delete=models.PROTECT, related_name="ratings")
    rating = models.IntegerField()  # Shown: max(0, mu - 3 sigma), scaled.
    mu = models.DecimalField(max_digits=12, decimal_places=6)  # The skill estimate.
    sigma = models.DecimalField(max_digits=12, decimal_places=6)  # Its uncertainty.
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
    """The append-only ledger. A player's rating history is this table, in order.

    Deltas are exact decimals, so the start plus the sum of a player's deltas is the projection.
    """

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
    delta = models.IntegerField()  # In the shown rating.
    rating_after = models.IntegerField()
    expected = models.FloatField(null=True, blank=True)
    team_avg = models.FloatField(null=True, blank=True)
    opp_avg = models.FloatField(null=True, blank=True)
    mu_delta = models.DecimalField(max_digits=12, decimal_places=6)
    sigma_delta = models.DecimalField(max_digits=12, decimal_places=6)
    mu_after = models.DecimalField(max_digits=12, decimal_places=6)
    sigma_after = models.DecimalField(max_digits=12, decimal_places=6)
    # The MVP or key-player share of mu_delta, zero-sum within the team.
    award_adj = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    cap_applied = models.BooleanField(default=False)
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
