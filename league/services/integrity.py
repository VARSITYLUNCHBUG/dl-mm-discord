"""Invariant checks over the rating ledger, used by `check_ledger` and the tests."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum

from league.models import MatchParticipant, PlayerRating, RatingEvent, Season
from league.services.corrections import diff_against_ledger, fold_season
from league.services.ratings import initial_state

ZERO = Decimal(0)


def check_season(season: Season) -> list[str]:
    # MVP and key-player shares must cancel out within each team.
    problems: list[str] = _award_problems(season)

    # The projection must equal the start plus the ledger for each player.
    start = initial_state(season)
    ledger = {
        player_id: (total or 0, mu or ZERO, sigma or ZERO)
        for player_id, total, mu, sigma in RatingEvent.objects.filter(season=season)
        .values("player_id")
        .annotate(total=Sum("delta"), mu=Sum("mu_delta"), sigma=Sum("sigma_delta"))
        .values_list("player_id", "total", "mu", "sigma")
    }
    for row in PlayerRating.objects.filter(season=season):
        total, mu, sigma = ledger.get(row.player_id, (0, ZERO, ZERO))
        if row.rating != start.rating + total:
            problems.append(
                f"Player {row.player_id}: projection {row.rating}, "
                f"ledger says {start.rating + total}."
            )
        if row.mu != start.mu + mu:
            problems.append(
                f"Player {row.player_id}: projection mu {row.mu}, ledger says {start.mu + mu}."
            )
        if row.sigma != start.sigma + sigma:
            problems.append(
                f"Player {row.player_id}: projection sigma {row.sigma}, "
                f"ledger says {start.sigma + sigma}."
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
    return problems


def _award_problems(season: Season) -> list[str]:
    """MVP and key-player adjustments must net to exactly zero within each team."""
    teams = {
        (match_id, player_id): team
        for match_id, player_id, team in MatchParticipant.objects.filter(
            match__season=season
        ).values_list("match_id", "player_id", "team")
    }
    sums: dict[tuple[int, str], Decimal] = defaultdict(lambda: ZERO)
    rows = RatingEvent.objects.filter(season=season, award_adj__isnull=False).values_list(
        "match_id", "player_id", "award_adj"
    )
    for match_id, player_id, adj in rows:
        if adj is None:
            continue
        sums[(match_id, teams.get((match_id, player_id), "?"))] += adj
    return [
        f"Match {match_id} team {team}: awards sum to {total}, not 0."
        for (match_id, team), total in sorted(sums.items())
        if total != 0
    ]


def check_all() -> list[str]:
    problems: list[str] = []
    for season in Season.objects.all():
        problems.extend(f"{season}: {problem}" for problem in check_season(season))
    return problems
