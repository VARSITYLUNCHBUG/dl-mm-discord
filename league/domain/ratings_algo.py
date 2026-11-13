"""The shapes the rating algorithm works in, shared by live rating, corrections and previews.

There is one algorithm, `openskill_pl_v1` in `league.domain.openskill_v1`. These types keep the
ledger code free of its details.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

SIX_PLACES = Decimal("0.000001")


def d6(value: float | Decimal | int) -> Decimal:
    """Pinned rounding for stored mu and sigma: six places, halves away from zero. Replays
    must reproduce past results exactly, so this is part of the algorithm."""
    return Decimal(value).quantize(SIX_PLACES, rounding=ROUND_HALF_UP)


def round_half_up(value: float | Decimal) -> int:
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class PlayerState:
    rating: int  # What players see: max(0, mu - 3 sigma), scaled.
    mu: Decimal
    sigma: Decimal
    games: int = 0
    last_played_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MatchInput:
    team_a: tuple[int, ...]
    team_b: tuple[int, ...]
    outcome: str  # "A" or "B".
    awards: Mapping[int, str] = field(default_factory=dict)  # player ID to "mvp" or "key".
    ended_at: datetime | None = None
    # Award holders whose bonus is frozen for MVP farming: see `openskill_v1.farming_frozen`.
    frozen: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class PlayerChange:
    delta: int  # Change in the displayed rating.
    expected: float  # This player's team's chance of winning, before the match.
    team_avg: float
    opp_avg: float
    mu_delta: Decimal
    sigma_delta: Decimal
    award_adj: Decimal | None = None
    cap_applied: bool = False

    def applied_to(self, state: PlayerState, when: datetime | None) -> PlayerState:
        return replace(
            state,
            rating=state.rating + self.delta,
            mu=state.mu + self.mu_delta,
            sigma=state.sigma + self.sigma_delta,
            games=state.games + 1,
            last_played_at=when,
        )


@dataclass(frozen=True, slots=True)
class Adjustment:
    """A staff seed or adjustment, as ledger deltas."""

    delta: int
    mu_delta: Decimal
    sigma_delta: Decimal

    def applied_to(self, state: PlayerState) -> PlayerState:
        return replace(
            state,
            rating=state.rating + self.delta,
            mu=state.mu + self.mu_delta,
            sigma=state.sigma + self.sigma_delta,
        )
