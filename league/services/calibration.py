"""How well the ratings predict results. Shared by `rating_calibration` and the staff dashboard.

Read-only: everything here refolds a season in memory and writes nothing. With few games the
numbers are noisy: at 200 matches a side check has a 95% interval of roughly plus or minus 7
points, so act only on large, consistent differences.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db.models import Count

from league.domain.openskill_v1 import OpenSkillAlgo, in_placement, merged_params
from league.domain.ratings_algo import MatchInput, PlayerState
from league.models import (
    ApiMatchCheck,
    Match,
    MatchAward,
    MatchParticipant,
    Player,
    PlayerRating,
    ScoreboardRead,
    Season,
)
from league.services.corrections import Fold, _rosters, fold_season
from league.services.ratings import awards_by_match

ALGO = OpenSkillAlgo()


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a rate. Honest at small counts, unlike the normal approximation."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return max(0.0, centre - half), min(1.0, centre + half)


def scores(pairs: list[tuple[float, int]]) -> tuple[float, float]:
    """Brier score and log loss of (predicted chance, 1 if it happened)."""
    if not pairs:
        return 0.0, 0.0
    brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    eps = 1e-9
    loss = -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in pairs)
    return brier, loss / len(pairs)


@dataclass(frozen=True, slots=True)
class Bin:
    low: float  # 0.4 means predictions from 40% to 50%.
    high: float
    games: int
    wins: int
    mean_predicted: float

    @property
    def rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.wins, self.games)


@dataclass(frozen=True, slots=True)
class Rate:
    hits: int
    total: int

    @property
    def share(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.hits, self.total)


@dataclass(frozen=True, slots=True)
class Mean:
    """A mean with an approximate 95% interval."""

    value: float
    n: int
    low: float
    high: float


def _mean(values: list[float]) -> Mean | None:
    if not values:
        return None
    m = statistics.fmean(values)
    half = 1.96 * statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else 1.0
    return Mean(m, len(values), m - half, m + half)


@dataclass(frozen=True, slots=True)
class AwardSignal:
    """Whether awards point to skill the rating hasn't caught up with yet.

    For each player in each rated match, the residual of their next few matches: won (1) or lost
    (0) minus their team's predicted chance. If MVP and key players are better than their rating
    says, their next matches beat the prediction more than everyone else's do. Matches overlap, so
    the intervals are optimistic; read them as rough until there are a few hundred matches.
    """

    horizon: int
    holders: Mean | None
    others: Mean | None

    @property
    def difference(self) -> Mean | None:
        if self.holders is None or self.others is None:
            return None
        diff = self.holders.value - self.others.value
        half = math.hypot(
            (self.holders.high - self.holders.low) / 2, (self.others.high - self.others.low) / 2
        )
        return Mean(diff, self.holders.n, diff - half, diff + half)


AWARD_HORIZON = 5


def award_signal(fold: Fold, rosters: dict[int, dict[int, str]], awards: dict[int, dict[int, str]],
                 horizon: int = AWARD_HORIZON) -> AwardSignal:  # fmt: skip
    history: dict[int, list[tuple[int, float]]] = {}
    for match_id in fold.order:
        for player_id, team in rosters.get(match_id, {}).items():
            change = fold.events.get((match_id, player_id))
            if change is None:
                continue
            won = 1.0 if team == fold.outcomes[match_id] else 0.0
            history.setdefault(player_id, []).append((match_id, won - change.expected))
    holders: list[float] = []
    others: list[float] = []
    for player_id, rows in history.items():
        for index, (match_id, _residual) in enumerate(rows):
            future = [residual for _m, residual in rows[index + 1 : index + 1 + horizon]]
            if len(future) < min(3, horizon):
                continue
            target = holders if awards.get(match_id, {}).get(player_id) else others
            target.append(statistics.fmean(future))
    return AwardSignal(horizon, _mean(holders), _mean(others))


def farming_watch(fold: Fold, params: dict[str, Any]) -> list[tuple[int, int, int]]:
    """(player ID, MVP awards, rated matches) for players whose award share is frozen now."""
    minimum = int(params["farming_min_games"])
    ceiling = float(params["farming_ratio"]) * float(params["mvp_expected"])
    out = []
    for player_id, state in fold.states.items():
        mvps = fold.mvps.get(player_id, 0)
        if state.games >= minimum and mvps / state.games > ceiling:
            out.append((player_id, mvps, state.games))
    return sorted(out, key=lambda row: -row[1] / row[2])


@dataclass(slots=True)
class Report:
    season: Season
    matches: int = 0
    brier: float = 0.0
    log_loss: float = 0.0
    bins: list[Bin] = field(default_factory=list)
    hidden_king: Rate = field(default_factory=lambda: Rate(0, 0))
    favourite: Rate = field(default_factory=lambda: Rate(0, 0))  # Team rated over 50% won.
    lopsided: int = 0  # Matches predicted outside 40-60%.
    sigma_bins: list[tuple[str, int]] = field(default_factory=list)
    placement: int = 0
    players: int = 0
    award_sources: dict[str, int] = field(default_factory=dict)
    top_mvps: list[tuple[str, int]] = field(default_factory=list)
    ocr: dict[str, int] = field(default_factory=dict)
    api: dict[str, int] = field(default_factory=dict)
    api_hit: Rate = field(default_factory=lambda: Rate(0, 0))
    awards_signal: AwardSignal | None = None
    farming: list[tuple[str, int, int]] = field(default_factory=list)  # name, MVPs, matches
    frozen_matches: int = 0
    left_early: int = 0


BRIER_BASELINE = 0.25
LOG_LOSS_BASELINE = math.log(2)


def report(season: Season, params: dict[str, Any] | None = None) -> Report:
    fold = fold_season(season, params_override=params)
    out = Report(season=season)
    pairs = [(p, 1 if fold.outcomes[m] == "A" else 0) for m, p in fold.expected_a.items()]
    out.matches = len(pairs)
    out.brier, out.log_loss = scores(pairs)
    buckets: dict[int, list[tuple[float, int]]] = {}
    for p, y in pairs:
        buckets.setdefault(min(int(p * 10), 9), []).append((p, y))
    out.bins = [
        Bin(
            low / 10,
            (low + 1) / 10,
            len(rows),
            sum(y for _p, y in rows),
            statistics.fmean(p for p, _y in rows),
        )
        for low, rows in sorted(buckets.items())
    ]
    out.favourite = Rate(
        sum(1 for p, y in pairs if (p > 0.5 and y == 1) or (p < 0.5 and y == 0)),
        sum(1 for p, _y in pairs if p != 0.5),
    )
    out.lopsided = sum(1 for p, _y in pairs if not 0.4 <= p <= 0.6)
    sides = Match.objects.filter(pk__in=list(fold.outcomes)).values_list("pk", "hidden_king_team")
    known = [(pk, hk) for pk, hk in sides if hk]
    out.hidden_king = Rate(sum(1 for pk, hk in known if fold.outcomes[pk] == hk), len(known))

    params_now = merged_params({**season.rating_params, **(params or {})})
    edges = [2, 3, 4, 5, 6, 7, 8, 9]
    counts: Counter[str] = Counter()
    rows = PlayerRating.objects.filter(season=season).values_list("sigma", "games")
    for sigma, games in rows:
        value = float(sigma)
        label = next((f"<{edge}" for edge in edges if value < edge), f">={edges[-1]}")
        counts[label] += 1
        out.players += 1
        if in_placement(games, params_now):
            out.placement += 1
    out.sigma_bins = [(f"<{edge}", counts.get(f"<{edge}", 0)) for edge in edges]
    out.sigma_bins.append((f">={edges[-1]}", counts.get(f">={edges[-1]}", 0)))

    awards = MatchAward.objects.filter(match__season=season, player__isnull=False)
    out.award_sources = dict(
        awards.values("source").annotate(n=Count("id")).values_list("source", "n")
    )
    out.top_mvps = Counter(
        awards.filter(kind="mvp").values_list("player__display_name", flat=True)
    ).most_common(5)
    out.ocr = dict(
        ScoreboardRead.objects.filter(match__season=season)
        .values("status")
        .annotate(n=Count("id"))
        .values_list("status", "n")
    )
    checks = ApiMatchCheck.objects.filter(match__season=season)
    out.api = dict(checks.values("status").annotate(n=Count("id")).values_list("status", "n"))
    with_id = checks.values("match_id").distinct().count()
    found = checks.exclude(status__in=("pending", "not_found", "error")).values("match_id")
    out.api_hit = Rate(found.distinct().count(), with_id)
    rosters = _rosters(season)
    out.awards_signal = award_signal(fold, rosters, dict(awards_by_match(fold.order)))
    names = dict(Player.objects.values_list("pk", "display_name"))
    out.farming = [
        (names.get(p, str(p)), mvps, games) for p, mvps, games in farming_watch(fold, params_now)
    ]
    out.frozen_matches = len(fold.frozen)
    out.left_early = MatchParticipant.objects.filter(match__season=season, left_early=True).count()
    return out


def simulate(matches: int, seed: int, params: dict[str, Any] | None = None) -> int | None:
    """Synthetic league: 24 players with known skill, one strong player seeded as new and in every
    match. Returns how many games they took to reach mu 30, or None if they didn't."""
    merged = merged_params(params)
    rng = random.Random(seed)
    true = {p: rng.gauss(25, 6) for p in range(24)}
    true[0] = 35.0
    states = {p: ALGO.initial(merged) for p in range(24)}
    states[0] = ALGO.seed(states[0], "new", merged).applied_to(states[0])
    for played in range(1, matches + 1):
        lobby = rng.sample(range(24), 12)
        if 0 not in lobby:
            lobby[0] = 0
        team_a, team_b = tuple(lobby[:6]), tuple(lobby[6:])
        strength = sum(true[p] for p in team_a) - sum(true[p] for p in team_b)
        a_wins = rng.random() < 1 / (1 + math.exp(-strength / 12))
        changes = ALGO.rate(states, MatchInput(team_a, team_b, "A" if a_wins else "B"), merged)
        for p, change in changes.items():
            states[p] = change.applied_to(states[p], None)
        if states[0].mu >= Decimal(30):
            return played
    return None


__all__ = ["BRIER_BASELINE", "LOG_LOSS_BASELINE", "PlayerState", "Report", "report", "simulate"]
