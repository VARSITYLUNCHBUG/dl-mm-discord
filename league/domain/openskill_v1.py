"""openskill_pl_v1: OpenSkill Plackett-Luce with the league's post-processing.

Each player has a skill estimate mu and an uncertainty sigma. After the library's update:

1. A player's first `placement_games` rated matches are placement. During placement, |delta mu|
   is capped at `cap_mu`, so one fluke game can't move a seed a whole band.
2. After placement, MVP and key players get a larger share of a win, or a smaller share of a loss.
   It is zero-sum within the team, so awards reshape a team's update without inflating it, and
   each player's adjustment is capped.
3. Sigma never drops below `sigma_floor`, so established players can still move.

A player whose MVP rate, over at least `farming_min_games` matches, is more than `farming_ratio`
times what chance alone gives (`mvp_expected`, one in twelve) keeps playing normally but gets no
award share until the rate falls back. Staff are told, so they can look into it.

Placement counts games, not sigma: with twelve players a match, sigma shrinks too slowly to be a
useful switch (about 7.1 after two dozen games, from 8.33).

Before rating, sigma grows for inactivity: by `decay_step` (in quadrature) per full
`decay_days`, up to `decay_max`. It never shrinks here.

Everything stored is rounded to six places with a pinned rule, so a replay is exact. The displayed
rating is max(0, mu - 3 sigma) times `display_scale`, a conservative estimate: a new player shows
0 and climbs as the system becomes sure of them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import Any

from openskill.models import PlackettLuce

from league.domain.ratings_algo import (
    Adjustment,
    MatchInput,
    PlayerChange,
    PlayerState,
    d6,
    round_half_up,
)

ALGO_VERSION = "openskill_pl_v1"

DEFAULT_PARAMS: dict[str, Any] = {
    "mu": 25.0,
    "sigma": 25 / 3,
    "beta": 25 / 6,
    "tau": 25 / 300,
    "cap_mu": 3.0,
    "award_cap": 0.5,
    "awards": {
        "win": {"mvp": 1.20, "key": 1.10},
        "loss": {"mvp": 0.80, "key": 0.90},
    },
    "sigma_floor": 2.0,
    "decay_days": 14,
    "decay_step": 0.5,
    "decay_max": 6.0,
    "display_scale": 40,
    "seeds": {"new": 15.0, "mid": 25.0, "high": 35.0},
    # Rated matches before a player leaves placement: the cap stops and awards start.
    "placement_games": 10,
    # Anti-farming: the award share is frozen while the MVP rate is this far above chance.
    "farming_min_games": 20,
    "farming_ratio": 2.0,
    "mvp_expected": 1 / 12,
    # Displayed-rating floor of each tier, in mu - 3 sigma units (before display_scale).
    "tiers": [["Bronze", 0], ["Silver", 10], ["Gold", 18], ["Platinum", 25], ["Diamond", 30]],
}


def merged_params(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    params = {**DEFAULT_PARAMS}
    for key, value in (stored or {}).items():
        if isinstance(params.get(key), dict) and isinstance(value, dict):
            params[key] = {**params[key], **value}
        else:
            params[key] = value
    return params


def display(mu: Decimal, sigma: Decimal, params: Mapping[str, Any]) -> int:
    conservative = max(Decimal(0), mu - 3 * sigma)
    return round_half_up(conservative * Decimal(str(params["display_scale"])))


def tier(mu: Decimal, sigma: Decimal, params: Mapping[str, Any]) -> str:
    value = float(mu - 3 * sigma)
    name = params["tiers"][0][0]
    for label, floor in params["tiers"]:
        if value >= floor:
            name = label
    return str(name)


def farming_frozen(
    awards: Mapping[int, str],
    history: Mapping[int, tuple[int, int]],
    params: Mapping[str, Any],
) -> frozenset[int]:
    """Award holders whose bonus is frozen. `history` is each player's rated matches and MVP
    awards before this match."""
    minimum = int(params["farming_min_games"])
    ceiling = float(params["farming_ratio"]) * float(params["mvp_expected"])
    frozen = set()
    for player_id in awards:
        games, mvps = history.get(player_id, (0, 0))
        if games >= minimum and mvps / games > ceiling:
            frozen.add(player_id)
    return frozenset(frozen)


def in_placement(games: int, params: Mapping[str, Any]) -> bool:
    """Whether a player with this many rated matches is still placing."""
    return games < int(params["placement_games"])


def _model(params: Mapping[str, Any]) -> PlackettLuce:
    return PlackettLuce(
        mu=float(params["mu"]),
        sigma=float(params["sigma"]),
        beta=float(params["beta"]),
        tau=float(params["tau"]),
        limit_sigma=False,
    )


def grown_sigma(state: PlayerState, when: Any, params: Mapping[str, Any]) -> Decimal:
    """Sigma after inactivity growth, at the moment `when` the match ended."""
    assert state.sigma is not None
    if state.last_played_at is None or when is None:
        return state.sigma
    periods = (when - state.last_played_at) // timedelta(days=int(params["decay_days"]))
    if periods <= 0:
        return state.sigma
    sigma = float(state.sigma)
    grown = math.sqrt(sigma**2 + periods * float(params["decay_step"]) ** 2)
    return d6(max(sigma, min(grown, float(params["decay_max"]))))


class OpenSkillAlgo:
    version = ALGO_VERSION

    def default_params(self) -> dict[str, Any]:
        return merged_params(None)

    def initial(self, params: Mapping[str, Any]) -> PlayerState:
        mu, sigma = d6(params["mu"]), d6(params["sigma"])
        return PlayerState(rating=display(mu, sigma, params), mu=mu, sigma=sigma)

    def _team(
        self, model: PlackettLuce, states: Mapping[int, PlayerState], ids: tuple[int, ...],
        sigmas: Mapping[int, Decimal],
    ) -> list[Any]:  # fmt: skip
        return [
            model.rating(mu=float(states[p].mu or 0), sigma=float(sigmas[p]), name=str(p))
            for p in ids
        ]

    def expected_a(
        self,
        states: Mapping[int, PlayerState],
        team_a: tuple[int, ...],
        team_b: tuple[int, ...],
        params: Mapping[str, Any],
    ) -> float:
        model = _model(params)
        sigmas = {p: states[p].sigma or d6(params["sigma"]) for p in (*team_a, *team_b)}
        teams = [
            self._team(model, states, team_a, sigmas),
            self._team(model, states, team_b, sigmas),
        ]
        return float(model.predict_win(teams)[0])

    def rate(
        self, states: Mapping[int, PlayerState], match: MatchInput, params: Mapping[str, Any]
    ) -> dict[int, PlayerChange]:
        model = _model(params)
        everyone = (*match.team_a, *match.team_b)
        pre_sigma = {p: grown_sigma(states[p], match.ended_at, params) for p in everyone}
        team_a = self._team(model, states, match.team_a, pre_sigma)
        team_b = self._team(model, states, match.team_b, pre_sigma)
        expected_a = float(model.predict_win([team_a, team_b])[0])
        ranks = [0.0, 1.0] if match.outcome == "A" else [1.0, 0.0]
        rated = model.rate([team_a, team_b], ranks=ranks)
        after = {int(str(r.name)): (float(r.mu), float(r.sigma)) for team in rated for r in team}

        base: dict[int, Decimal] = {}
        capped: set[int] = set()
        cap_mu = Decimal(str(params["cap_mu"]))
        placing = {p for p in everyone if in_placement(states[p].games, params)}
        for p in everyone:
            delta = d6(after[p][0]) - (states[p].mu or Decimal(0))
            if p in placing and abs(delta) > cap_mu:
                delta = cap_mu if delta > 0 else -cap_mu
                capped.add(p)
            base[p] = delta

        adj: dict[int, Decimal] = dict.fromkeys(everyone, Decimal(0))
        for team, won in (
            (match.team_a, match.outcome == "A"),
            (match.team_b, match.outcome == "B"),
        ):
            no_award = placing | set(match.frozen)
            adj.update(_award_adjustments(team, won, base, no_award, match.awards, params))

        floor = Decimal(str(params["sigma_floor"]))
        avg = {
            "A": sum(states[p].rating for p in match.team_a) / len(match.team_a),
            "B": sum(states[p].rating for p in match.team_b) / len(match.team_b),
        }
        changes: dict[int, PlayerChange] = {}
        for p in everyone:
            state = states[p]
            assert state.mu is not None and state.sigma is not None
            mu_after = state.mu + base[p] + adj[p]
            sigma_after = max(d6(after[p][1]), floor)
            on_a = p in match.team_a
            changes[p] = PlayerChange(
                delta=display(mu_after, sigma_after, params) - state.rating,
                expected=expected_a if on_a else 1 - expected_a,
                team_avg=avg["A"] if on_a else avg["B"],
                opp_avg=avg["B"] if on_a else avg["A"],
                mu_delta=mu_after - state.mu,
                sigma_delta=sigma_after - state.sigma,
                award_adj=adj[p] if adj[p] else None,
                cap_applied=p in capped,
            )
        return changes

    def seed(self, state: PlayerState, value: int | str, params: Mapping[str, Any]) -> Adjustment:
        seeds = params["seeds"]
        if isinstance(value, str):
            if value not in seeds:
                raise ValueError(f"Seed band must be one of {', '.join(seeds)}.")
            mu = d6(seeds[value])
        else:
            mu = d6(value)
        sigma = d6(params["sigma"])
        assert state.mu is not None and state.sigma is not None
        return Adjustment(
            delta=display(mu, sigma, params) - state.rating,
            mu_delta=mu - state.mu,
            sigma_delta=sigma - state.sigma,
        )

    def adjust(self, state: PlayerState, delta: int, params: Mapping[str, Any]) -> Adjustment:
        """A staff adjustment in displayed points, applied to mu."""
        assert state.mu is not None and state.sigma is not None
        mu_delta = d6(Decimal(delta) / Decimal(str(params["display_scale"])))
        rating = display(state.mu + mu_delta, state.sigma, params)
        return Adjustment(delta=rating - state.rating, mu_delta=mu_delta, sigma_delta=Decimal(0))

    def strength(self, state: PlayerState) -> float:
        # Sort by the estimate itself, not the conservative display: a high seed in placement
        # shows a low number but is not a weak player.
        return float(state.mu if state.mu is not None else state.rating)


def _award_adjustments(
    team: tuple[int, ...],
    won: bool,
    base: Mapping[int, Decimal],
    placing: set[int],
    awards: Mapping[int, str],
    params: Mapping[str, Any],
) -> dict[int, Decimal]:
    """Zero-sum reshaping of one team's update for its MVP and key players."""
    table = params["awards"]["win" if won else "loss"]
    limit = Decimal(str(params["award_cap"]))
    holders = [p for p in team if awards.get(p) in table and p not in placing and base[p] != 0]
    others = [p for p in team if p not in holders]
    others_total = sum((base[p] for p in others), Decimal(0))
    if not holders or not others or others_total == 0:
        return {}

    raw: dict[int, Decimal] = {}
    for p in holders:
        extra = base[p] * (Decimal(str(table[awards[p]])) - 1)
        raw[p] = max(-limit, min(limit, extra))
    total = sum(raw.values(), Decimal(0))
    # The rest of the team gives back the same amount, in proportion to their own update.
    shares = {p: -total * base[p] / others_total for p in others}
    worst = max(abs(value) for value in shares.values())
    if worst > limit:
        scale = limit / worst
        raw = {p: value * scale for p, value in raw.items()}
        shares = {p: value * scale for p, value in shares.items()}

    result = {p: d6(value) for p, value in raw.items()}
    ordered = sorted(others, key=lambda p: (-abs(base[p]), p))
    for p in ordered[1:]:
        result[p] = d6(shares[p])
    # Rounding leftovers go to the player with the largest update, so the sum is exactly zero.
    result[ordered[0]] = -sum(result.values(), Decimal(0))
    return result
