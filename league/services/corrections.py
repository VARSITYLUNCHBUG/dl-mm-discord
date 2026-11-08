"""Voids, rescores and late subs share one path: refold, diff, append only the differences.

Past draft decisions are facts and are never re-run. Only ratings are recomputed, in timeline
order, because one changed result shifts every later match its players were in.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

from django.db import transaction
from django.db.models import Sum

from league.domain import elo
from league.domain.engine import Status
from league.domain.errors import InvalidAction
from league.domain.mode_schema import parse_mode
from league.models import (
    Match,
    MatchParticipant,
    MatchResult,
    Player,
    PlayerRating,
    RatingEvent,
    RatingRun,
    Season,
)
from league.models.matches import RemovedReason, Source
from league.models.ratings import RunKind
from league.services.common import (
    add_effect,
    bump,
    lock_community,
    lock_match,
    now,
    render_board,
    roster,
    save_transition,
    transition,
)
from league.services.lobby import next_revision


@dataclass(slots=True)
class TargetEvent:
    delta: int
    expected: float
    team_avg: float
    opp_avg: float


@dataclass(slots=True)
class Fold:
    """What the ledger should say, given the current facts."""

    events: dict[tuple[int, int], TargetEvent] = field(default_factory=dict)
    ratings: dict[int, int] = field(default_factory=dict)
    games: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    wins: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    losses: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    last_played: dict[int, datetime | None] = field(default_factory=dict)


def _latest_outcomes(season: Season) -> dict[int, str]:
    latest: dict[int, str] = {}
    results = MatchResult.objects.filter(match__season=season).order_by("match_id", "revision")
    for match_id, outcome in results.values_list("match_id", "outcome"):
        latest[match_id] = outcome
    return latest


def _rosters(season: Season) -> dict[int, dict[int, str]]:
    rows = MatchParticipant.objects.filter(match__season=season, removed_reason="").exclude(team="")
    result: dict[int, dict[int, str]] = defaultdict(dict)
    for match_id, player_id, team in rows.values_list("match_id", "player_id", "team"):
        result[match_id][player_id] = team
    return result


def fold_season(
    season: Season,
    *,
    outcome_override: dict[int, str] | None = None,
    roster_override: dict[int, dict[int, str]] | None = None,
) -> Fold:
    """Replay every rated match and manual adjustment in order, from the start rating.

    `outcome_override` and `roster_override` make this usable for previews before any write.
    """
    outcomes = {**_latest_outcomes(season), **(outcome_override or {})}
    rosters = {**_rosters(season), **(roster_override or {})}

    timeline: list[tuple[datetime, str, int, Match | RatingEvent]] = []
    matches = Match.objects.filter(season=season, ended_at__isnull=False)
    for match in matches.only("id", "ended_at"):
        if outcomes.get(match.pk) in {"A", "B"}:
            timeline.append((cast(datetime, match.ended_at), "match", match.pk, match))
    adjustments = RatingEvent.objects.filter(
        season=season, run__kind__in=(RunKind.SEED, RunKind.ADMIN)
    ).select_related("run")
    for event in adjustments:
        timeline.append((event.created_at, "adjust", event.pk, event))
    timeline.sort(key=lambda entry: entry[:3])

    fold = Fold()

    def rating(player_id: int) -> int:
        return fold.ratings.get(player_id, season.start_rating)

    for _at, _kind, _pk, item in timeline:
        if isinstance(item, RatingEvent):
            fold.ratings[item.player_id] = rating(item.player_id) + item.delta
            continue
        match_roster = rosters.get(item.pk, {})
        team_a = sorted(p for p, team in match_roster.items() if team == "A")
        team_b = sorted(p for p, team in match_roster.items() if team == "B")
        if not team_a or not team_b:
            continue
        outcome = outcomes[item.pk]
        result = elo.compute(
            [rating(p) for p in team_a],
            [rating(p) for p in team_b],
            a_won=outcome == "A",
            k=season.k_factor,
        )
        for player_id in [*team_a, *team_b]:
            on_a = match_roster[player_id] == "A"
            delta = result.delta_a if on_a else -result.delta_a
            fold.ratings[player_id] = rating(player_id) + delta
            fold.events[(item.pk, player_id)] = TargetEvent(
                delta=delta,
                expected=result.expected_a if on_a else 1 - result.expected_a,
                team_avg=result.avg_a if on_a else result.avg_b,
                opp_avg=result.avg_b if on_a else result.avg_a,
            )
            fold.games[player_id] += 1
            won = on_a == (outcome == "A")
            fold.wins[player_id] += 1 if won else 0
            fold.losses[player_id] += 0 if won else 1
            fold.last_played[player_id] = item.ended_at
    return fold


def _ledger_totals(season: Season) -> dict[tuple[int, int], int]:
    rows = (
        RatingEvent.objects.filter(season=season, match__isnull=False)
        .values("match_id", "player_id")
        .annotate(total=Sum("delta"))
        .values_list("match_id", "player_id", "total")
    )
    return {(match_id, player_id): total for match_id, player_id, total in rows}


def diff_against_ledger(season: Season, fold: Fold) -> dict[tuple[int, int], int]:
    """(match, player) to the delta still needed to make the ledger match the fold."""
    ledger = _ledger_totals(season)
    diffs: dict[tuple[int, int], int] = {}
    for key in set(ledger) | set(fold.events):
        target = fold.events[key].delta if key in fold.events else 0
        needed = target - ledger.get(key, 0)
        if needed:
            diffs[key] = needed
    return diffs


@dataclass(frozen=True, slots=True)
class Preview:
    """Net rating change per player, and how many ledger rows the correction would write."""

    net_by_player: dict[int, int]
    events_to_write: int


def _preview(fold: Fold, diffs: dict[tuple[int, int], int]) -> Preview:
    net: dict[int, int] = defaultdict(int)
    for (_match_id, player_id), delta in diffs.items():
        net[player_id] += delta
    return Preview(net_by_player={p: d for p, d in net.items() if d}, events_to_write=len(diffs))


def preview_outcome(match: Match, outcome: str) -> Preview:
    """What changing this match's result would do. Writes nothing."""
    if outcome not in {"A", "B", "void"}:
        raise InvalidAction("Outcome must be A, B or void.")
    fold = fold_season(match.season, outcome_override={match.pk: outcome})
    return _preview(fold, diff_against_ledger(match.season, fold))


def preview_sub(match: Match, out_player: Player, in_player: Player) -> Preview:
    current = {p.player_id: p.team for p in roster(match)}
    if out_player.pk not in current:
        raise InvalidAction(f"{out_player.display_name} is not on this match's roster.")
    if in_player.pk in current:
        raise InvalidAction(f"{in_player.display_name} is already on this match's roster.")
    current[in_player.pk] = current.pop(out_player.pk)
    fold = fold_season(match.season, roster_override={match.pk: current})
    return _preview(fold, diff_against_ledger(match.season, fold))


def _write_correction(
    season: Season, match: Match, revision_note: str, actor: Player | None, reason: str
) -> RatingRun | None:
    """Append the differences, in one run, then rebuild the projection from the fold."""
    fold = fold_season(season)
    diffs = diff_against_ledger(season, fold)
    run = None
    created_at = now()
    if diffs:
        run = RatingRun.objects.create(
            idempotency_key=f"correction:{match.pk}:{revision_note}",
            kind=RunKind.CORRECTION,
            season=season,
            match=match,
            algo_version=season.algo_version,
            params={"k": season.k_factor, "divisor": elo.DIVISOR},
            actor=actor,
            reason=reason[:500],
            created_at=created_at,
        )
        running = dict(
            PlayerRating.objects.filter(season=season).values_list("player_id", "rating")
        )
        events = []
        for (match_id, player_id), delta in sorted(diffs.items()):
            running[player_id] = running.get(player_id, season.start_rating) + delta
            target = fold.events.get((match_id, player_id))
            events.append(
                RatingEvent(
                    run=run,
                    season=season,
                    player_id=player_id,
                    match_id=match_id,
                    delta=delta,
                    rating_after=running[player_id],
                    expected=target.expected if target else None,
                    k=season.k_factor,
                    team_avg=target.team_avg if target else None,
                    opp_avg=target.opp_avg if target else None,
                    created_at=created_at,
                )
            )
        RatingEvent.objects.bulk_create(events)
    rebuild_projection(season, fold)
    return run


def rebuild_projection(season: Season, fold: Fold | None = None) -> int:
    """Make `player_ratings` agree with the ledger and the facts. Returns rows touched."""
    fold = fold or fold_season(season)
    ledger = dict(
        RatingEvent.objects.filter(season=season)
        .values("player_id")
        .annotate(total=Sum("delta"))
        .values_list("player_id", "total")
    )
    touched = 0
    for player_id, total in ledger.items():
        PlayerRating.objects.update_or_create(
            season=season,
            player_id=player_id,
            defaults={
                "rating": season.start_rating + total,
                "games": fold.games.get(player_id, 0),
                "wins": fold.wins.get(player_id, 0),
                "losses": fold.losses.get(player_id, 0),
                "last_played_at": fold.last_played.get(player_id),
            },
        )
        touched += 1
    return touched


def _has_full_teams(match: Match) -> bool:
    """A match voided mid-draft has partial rosters, so it can never be rated."""
    size = parse_mode(match.mode_version.body).team_size
    return all(roster(match).filter(team=team).count() == size for team in ("A", "B"))


@transaction.atomic
def apply_outcome_correction(
    match_id: int, outcome: str, actor: Player | None, reason: str
) -> RatingRun | None:
    """Change the result of a match that has ended. Appends; never rewrites history."""
    match = lock_match(match_id)
    lock_community(match.community_id)  # Corrections and live results must not interleave.
    if match.status not in {Status.COMPLETED, Status.VOIDED}:
        raise InvalidAction("Only a finished match can be corrected.")
    if outcome not in {"A", "B", "void"}:
        raise InvalidAction("Outcome must be A, B or void.")
    if outcome == "void" and match.status == Status.VOIDED:
        raise InvalidAction("That match is already void.")
    if outcome != "void" and match.winner == outcome:
        raise InvalidAction("That team is already the recorded winner.")
    if outcome != "void" and not _has_full_teams(match):
        raise InvalidAction(
            "That match was voided before teams were drafted, so it can't be rated."
        )

    revision = next_revision(match)
    MatchResult.objects.create(
        match=match,
        revision=revision,
        outcome=outcome,
        source=Source.STAFF,
        actor=actor,
        reason=reason[:500],
        created_at=now(),
    )
    target = Status.VOIDED if outcome == "void" else Status.COMPLETED
    if target != match.status:
        transition(match, target, source=Source.STAFF, actor=actor)
    match.winner = "" if outcome == "void" else outcome
    if match.ended_at is None:
        match.ended_at = now()
    save_transition(match, "winner", "ended_at")
    run = _write_correction(match.season, match, f"rev:{revision}", actor, reason)
    add_effect(
        match.community,
        "post_correction",
        {"reason": reason[:300], "outcome": outcome, "run_id": run.pk if run else None},
        match=match,
        dedupe_key=f"post_correction:{match.pk}:{revision}",
    )
    render_board(match)
    return run


@transaction.atomic
def apply_sub_correction(
    match_id: int, out_player: Player, in_player: Player, actor: Player | None, reason: str
) -> RatingRun | None:
    """Replace a player on a finished match's roster. The substitute takes the result."""
    match = lock_match(match_id)
    lock_community(match.community_id)
    if match.status != Status.COMPLETED:
        raise InvalidAction("Late substitutions apply only to a completed match.")
    leaving = roster(match).filter(player=out_player).first()
    if leaving is None:
        raise InvalidAction(f"{out_player.display_name} is not on this match's roster.")
    if match.participants.filter(player=in_player).exists():
        raise InvalidAction(f"{in_player.display_name} was already part of this match.")
    moment = now()
    MatchParticipant.objects.create(
        match=match,
        player=in_player,
        queue_joined_at=leaving.queue_joined_at,
        ready_at=leaving.ready_at,
        team=leaving.team,
        draft_pick_number=leaving.draft_pick_number,
        is_active=False,
    )
    leaving.removed_reason = RemovedReason.SUBBED_OUT
    leaving.removed_at = moment
    leaving.is_captain = False
    leaving.save(update_fields=["removed_reason", "removed_at", "is_captain"])
    bump(match)
    run = _write_correction(match.season, match, f"sub:{match.version}", actor, reason)
    add_effect(
        match.community,
        "post_correction",
        {"reason": reason[:300], "outcome": "sub", "run_id": run.pk if run else None},
        match=match,
    )
    return run
