"""Invariant checks over the rating ledger, used by `check_ledger` and the tests."""

from __future__ import annotations

from django.db.models import Sum

from league.models import PlayerRating, RatingEvent, RatingRun, Season
from league.models.ratings import RunKind
from league.services.corrections import diff_against_ledger, fold_season


def check_season(season: Season) -> list[str]:
    problems: list[str] = []

    # Every match-based run must be exactly zero-sum.
    runs = RatingRun.objects.filter(season=season, kind__in=(RunKind.LIVE, RunKind.CORRECTION))
    for run in runs:
        total = run.events.aggregate(total=Sum("delta"))["total"] or 0
        if total != 0:
            problems.append(f"Run {run.idempotency_key} sums to {total}, not 0.")

    # The projection must equal the start rating plus the ledger for each player.
    ledger = dict(
        RatingEvent.objects.filter(season=season)
        .values("player_id")
        .annotate(total=Sum("delta"))
        .values_list("player_id", "total")
    )
    for row in PlayerRating.objects.filter(season=season):
        expected = season.start_rating + ledger.get(row.player_id, 0)
        if row.rating != expected:
            problems.append(
                f"Player {row.player_id}: projection {row.rating}, ledger says {expected}."
            )
    missing = set(ledger) - set(
        PlayerRating.objects.filter(season=season).values_list("player_id", flat=True)
    )
    if missing:
        problems.append(f"Ledger players with no rating row: {sorted(missing)}.")

    # Replaying the facts from scratch must reproduce the ledger exactly.
    fold = fold_season(season)
    differences = diff_against_ledger(season, fold)
    if differences:
        problems.append(f"Replay differs from the ledger on {len(differences)} entries.")

    # Everything except manual adjustments nets to zero across the season.
    adjustments = (
        RatingEvent.objects.filter(
            season=season, run__kind__in=(RunKind.SEED, RunKind.ADMIN)
        ).aggregate(total=Sum("delta"))["total"]
        or 0
    )
    total_delta = sum(ledger.values())
    if total_delta != adjustments:
        problems.append(f"Ledger total {total_delta} does not equal adjustments {adjustments}.")
    return problems


def check_all() -> list[str]:
    problems: list[str] = []
    for season in Season.objects.all():
        problems.extend(f"{season}: {problem}" for problem in check_season(season))
    return problems
