"""Applying results to the ledger. One transaction with no Discord calls, safe to retry.

Ratings are OpenSkill Plackett-Luce (`league.domain.openskill_v1`), with each season's own
parameters. This module loads states, asks the algorithm for changes and writes them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Max, Sum

from league.domain.errors import InvalidAction
from league.domain.openskill_v1 import (
    ALGO_VERSION,
    OpenSkillAlgo,
    farming_frozen,
    in_placement,
    merged_params,
)
from league.domain.ratings_algo import Adjustment, MatchInput, PlayerChange, PlayerState
from league.models import (
    Match,
    MatchAward,
    MatchParticipant,
    Player,
    PlayerRating,
    RatingEvent,
    RatingRun,
    Season,
)
from league.models.ratings import RunKind
from league.services.common import now, roster

ALGO = OpenSkillAlgo()


def algo_of(season: Season) -> OpenSkillAlgo:
    return ALGO


def params_for(season: Season) -> dict[str, Any]:
    """The parameters a season rates with: its `rating_params` over the defaults."""
    return merged_params(season.rating_params)


def initial_state(season: Season) -> PlayerState:
    return ALGO.initial(params_for(season))


def state_of(season: Season, row: PlayerRating | None) -> PlayerState:
    if row is None:
        return initial_state(season)
    return PlayerState(
        rating=row.rating,
        mu=row.mu,
        sigma=row.sigma,
        games=row.games,
        last_played_at=row.last_played_at,
    )


def _rating_row(season: Season, player_id: int, *, lock: bool = True) -> PlayerRating:
    query = PlayerRating.objects.select_for_update() if lock else PlayerRating.objects
    row = query.filter(season=season, player_id=player_id).first()
    if row is not None:
        return row
    start = initial_state(season)
    return PlayerRating.objects.create(
        season=season, player_id=player_id, rating=start.rating, mu=start.mu, sigma=start.sigma
    )


@dataclass(frozen=True, slots=True)
class RosterPair:
    team_a: list[int]
    team_b: list[int]


def roster_pair(match: Match) -> RosterPair:
    rows = list(roster(match).values_list("team", "player_id"))
    return RosterPair(
        team_a=sorted(player_id for team, player_id in rows if team == "A"),
        team_b=sorted(player_id for team, player_id in rows if team == "B"),
    )


def current_awards(match: Match) -> dict[int, str]:
    """The MVP and key players in force: the rows of the highest award revision."""
    from league.services.awards import current

    return current(match)[0]


def event_fields(change: PlayerChange, after: PlayerState) -> dict[str, Any]:
    return {
        "delta": change.delta,
        "rating_after": after.rating,
        "expected": change.expected,
        "team_avg": change.team_avg,
        "opp_avg": change.opp_avg,
        "mu_delta": change.mu_delta,
        "sigma_delta": change.sigma_delta,
        "mu_after": after.mu,
        "sigma_after": after.sigma,
        "award_adj": change.award_adj,
        "cap_applied": change.cap_applied,
    }


def run_params(season: Season) -> dict[str, Any]:
    params = params_for(season)
    return {"algo": ALGO_VERSION, **{k: v for k, v in params.items() if k != "tiers"}}


def apply_live_result(match: Match, outcome: str, revision: int) -> RatingRun | None:
    """Rate a finished match from its final rosters. A second call with the same revision is a
    no-op, so a retried finalise can never double-count."""
    if outcome not in {"A", "B"}:
        return None
    season = match.season
    key = f"match:{match.pk}:rev:{revision}"
    existing = RatingRun.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing

    pair = roster_pair(match)
    # Lock rows in a fixed order so two finishing matches sharing players cannot deadlock.
    rows = {
        player_id: _rating_row(season, player_id)
        for player_id in sorted([*pair.team_a, *pair.team_b])
    }
    states = {player_id: state_of(season, row) for player_id, row in rows.items()}
    awards = current_awards(match)
    params = params_for(season)
    frozen = farming_frozen(awards, mvp_history(match, awards, states), params)
    changes = ALGO.rate(
        states,
        MatchInput(
            team_a=tuple(pair.team_a),
            team_b=tuple(pair.team_b),
            outcome=outcome,
            awards=awards,
            ended_at=match.ended_at,
            frozen=frozen,
        ),
        params,
    )
    _tell_staff_about_farming(match, frozen)
    created_at = now()
    run = RatingRun.objects.create(
        idempotency_key=key,
        kind=RunKind.LIVE,
        season=season,
        match=match,
        algo_version=ALGO_VERSION,
        params=run_params(season),
        created_at=created_at,
    )
    events = []
    for player_id, row in rows.items():
        change = changes[player_id]
        after = change.applied_to(states[player_id], match.ended_at or created_at)
        _store(row, after)
        on_a = player_id in pair.team_a
        won = on_a == (outcome == "A")
        row.games += 1
        row.wins += 1 if won else 0
        row.losses += 0 if won else 1
        row.last_played_at = match.ended_at or created_at
        events.append(
            RatingEvent(
                run=run,
                season=season,
                player_id=player_id,
                match=match,
                created_at=created_at,
                **event_fields(change, after),
            )
        )
    PlayerRating.objects.bulk_update(
        rows.values(), ["rating", "mu", "sigma", "games", "wins", "losses", "last_played_at"]
    )
    RatingEvent.objects.bulk_create(events)
    return run


def mvp_history(
    match: Match, awards: Mapping[int, str], states: Mapping[int, PlayerState]
) -> dict[int, tuple[int, int]]:
    """For this match's award holders: rated matches and MVP awards before it, this season. The
    same counts the refold keeps, so live and replayed ratings agree."""
    if not awards:
        return {}
    earlier = list(
        Match.objects.filter(season=match.season, status="completed")
        .exclude(pk=match.pk)
        .values_list("pk", flat=True)
    )
    counts: dict[int, int] = dict.fromkeys(awards, 0)
    rosters = set(
        MatchParticipant.objects.filter(match_id__in=earlier, removed_reason="")
        .exclude(team="")
        .values_list("match_id", "player_id")
    )
    for match_id, holders in awards_by_match(earlier).items():
        for player_id, kind in holders.items():
            if kind == "mvp" and player_id in counts and (match_id, player_id) in rosters:
                counts[player_id] += 1
    return {p: (states[p].games, counts[p]) for p in awards if p in states}


def _tell_staff_about_farming(match: Match, frozen: frozenset[int]) -> None:
    if not frozen:
        return
    from league.services.common import add_effect

    names = dict(Player.objects.filter(pk__in=frozen).values_list("pk", "display_name"))
    for player_id in sorted(frozen):
        add_effect(
            match.community,
            "ping_staff",
            {
                "text": f"froze {names.get(player_id, player_id)}'s award share: their MVP rate "
                "is far above chance. Worth a look; it lifts by itself if the rate falls."
            },
            match=match,
            dedupe_key=f"award_freeze:{match.season_id}:{player_id}",
        )


def _store(row: PlayerRating, state: PlayerState) -> None:
    row.rating = state.rating
    row.mu = state.mu
    row.sigma = state.sigma


@transaction.atomic
def seed_player(
    season: Season, player: Player, value: int | str, actor: Player | None, reason: str = ""
) -> RatingEvent:
    """Set a player's starting skill: a band (new, mid or high) or a mu. Resets their
    uncertainty to the start. Written to the ledger."""
    row = _rating_row(season, player.pk)
    try:
        adjustment = ALGO.seed(state_of(season, row), value, params_for(season))
    except ValueError as exc:
        raise InvalidAction(str(exc)) from exc
    return _adjust(season, row, adjustment, RunKind.SEED, actor, reason or f"seeded to {value}")


@transaction.atomic
def adjust_player(
    season: Season, player: Player, delta: int, actor: Player | None, reason: str
) -> RatingEvent:
    """Move a player's shown rating by `delta` points, through mu."""
    if not reason.strip():
        raise InvalidAction("An adjustment needs a reason.")
    row = _rating_row(season, player.pk)
    adjustment = ALGO.adjust(state_of(season, row), delta, params_for(season))
    return _adjust(season, row, adjustment, RunKind.ADMIN, actor, reason)


def _adjust(
    season: Season,
    row: PlayerRating,
    adjustment: Adjustment,
    kind: str,
    actor: Player | None,
    reason: str,
) -> RatingEvent:
    created_at = now()
    count = RatingRun.objects.filter(season=season, kind=kind).count()
    run = RatingRun.objects.create(
        idempotency_key=f"{kind}:{season.pk}:{row.player_id}:{count + 1}",
        kind=kind,
        season=season,
        algo_version=ALGO_VERSION,
        actor=actor,
        reason=reason[:500],
        created_at=created_at,
    )
    after = adjustment.applied_to(state_of(season, row))
    _store(row, after)
    row.save(update_fields=["rating", "mu", "sigma"])
    return RatingEvent.objects.create(
        run=run,
        season=season,
        player_id=row.player_id,
        delta=adjustment.delta,
        rating_after=after.rating,
        mu_delta=adjustment.mu_delta,
        sigma_delta=adjustment.sigma_delta,
        mu_after=after.mu,
        sigma_after=after.sigma,
        created_at=created_at,
    )


def match_deltas(match: Match) -> dict[int, int]:
    """Net rating change per player for a match, across all live and correction runs."""
    rows = (
        RatingEvent.objects.filter(match=match)
        .values("player_id")
        .annotate(total=Sum("delta"))
        .values_list("player_id", "total")
    )
    return dict(rows)


def expected_win(season: Season, team_a: list[int], team_b: list[int]) -> float | None:
    """Team A's chance before a match, from current ratings. None until both teams are full."""
    if not team_a or not team_b:
        return None
    rows = {
        row.player_id: row
        for row in PlayerRating.objects.filter(season=season, player_id__in=[*team_a, *team_b])
    }
    states = {p: state_of(season, rows.get(p)) for p in (*team_a, *team_b)}
    return ALGO.expected_a(states, tuple(team_a), tuple(team_b), params_for(season))


def awards_by_match(match_ids: list[int]) -> Mapping[int, dict[int, str]]:
    """Current awards for many matches at once, for the fold."""
    latest: dict[int, int] = dict(
        MatchAward.objects.filter(match_id__in=match_ids)
        .values("match_id")
        .annotate(latest=Max("revision"))
        .values_list("match_id", "latest")
    )
    result: dict[int, dict[int, str]] = {match_id: {} for match_id in latest}
    rows = MatchAward.objects.filter(match_id__in=list(latest), player__isnull=False)
    for match_id, revision, player_id, kind in rows.values_list(
        "match_id", "revision", "player_id", "kind"
    ):
        if revision == latest[match_id]:
            result[match_id][player_id] = kind
    return result


def placement_labels(season: Season, player_ids: list[int]) -> dict[int, str]:
    """ "P 3/10" for players whose rating is still settling."""
    params = params_for(season)
    total = int(params["placement_games"])
    games = dict(
        PlayerRating.objects.filter(season=season, player_id__in=player_ids).values_list(
            "player_id", "games"
        )
    )
    labels = {}
    for player_id in player_ids:
        played = games.get(player_id, 0)
        if in_placement(played, params):
            labels[player_id] = f"P {played}/{total}"
    return labels
