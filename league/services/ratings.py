"""Applying results to the ledger. One transaction with no Discord calls, safe to retry."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum

from league.domain import elo
from league.domain.errors import InvalidAction
from league.models import Match, Player, PlayerRating, RatingEvent, RatingRun, Season
from league.models.ratings import RunKind
from league.services.common import now, roster


def _rating_row(season: Season, player_id: int, *, lock: bool = True) -> PlayerRating:
    query = PlayerRating.objects.select_for_update() if lock else PlayerRating.objects
    row = query.filter(season=season, player_id=player_id).first()
    if row is not None:
        return row
    return PlayerRating.objects.create(
        season=season, player_id=player_id, rating=season.start_rating
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
    result = elo.compute(
        [rows[p].rating for p in pair.team_a],
        [rows[p].rating for p in pair.team_b],
        a_won=outcome == "A",
        k=season.k_factor,
    )
    created_at = now()
    run = RatingRun.objects.create(
        idempotency_key=key,
        kind=RunKind.LIVE,
        season=season,
        match=match,
        algo_version=season.algo_version,
        params={"k": season.k_factor, "divisor": elo.DIVISOR},
        created_at=created_at,
    )
    events = []
    for player_id, row in rows.items():
        on_a = player_id in pair.team_a
        delta = result.delta_a if on_a else -result.delta_a
        row.rating += delta
        row.games += 1
        won = on_a == (outcome == "A")
        row.wins += 1 if won else 0
        row.losses += 0 if won else 1
        row.last_played_at = created_at
        events.append(
            RatingEvent(
                run=run,
                season=season,
                player_id=player_id,
                match=match,
                delta=delta,
                rating_after=row.rating,
                expected=result.expected_a if on_a else 1 - result.expected_a,
                k=season.k_factor,
                team_avg=result.avg_a if on_a else result.avg_b,
                opp_avg=result.avg_b if on_a else result.avg_a,
                created_at=created_at,
            )
        )
    PlayerRating.objects.bulk_update(
        rows.values(), ["rating", "games", "wins", "losses", "last_played_at"]
    )
    RatingEvent.objects.bulk_create(events)
    return run


@transaction.atomic
def seed_player(
    season: Season, player: Player, rating: int, actor: Player | None, reason: str = ""
) -> RatingEvent:
    """Set a player's rating, for known skill or staff decisions. Written to the ledger."""
    row = _rating_row(season, player.pk)
    delta = rating - row.rating
    return _adjust(season, row, delta, RunKind.SEED, actor, reason or f"seeded to {rating}")


@transaction.atomic
def adjust_player(
    season: Season, player: Player, delta: int, actor: Player | None, reason: str
) -> RatingEvent:
    if not reason.strip():
        raise InvalidAction("An adjustment needs a reason.")
    return _adjust(season, _rating_row(season, player.pk), delta, RunKind.ADMIN, actor, reason)


def _adjust(
    season: Season,
    row: PlayerRating,
    delta: int,
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
        algo_version=season.algo_version,
        actor=actor,
        reason=reason[:500],
        created_at=created_at,
    )
    row.rating += delta
    row.save(update_fields=["rating"])
    return RatingEvent.objects.create(
        run=run,
        season=season,
        player_id=row.player_id,
        delta=delta,
        rating_after=row.rating,
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
