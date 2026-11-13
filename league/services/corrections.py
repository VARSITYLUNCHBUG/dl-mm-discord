"""Voids, rescores, late subs and award changes share one path: refold, diff, append only the
differences.

Past draft decisions are facts and are never re-run. Only ratings are recomputed, in timeline
order, because one changed result shifts every later match its players were in.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import cast

from django.db import transaction
from django.db.models import Sum

from league.domain.engine import Status
from league.domain.errors import InvalidAction
from league.domain.mode_schema import parse_mode
from league.domain.openskill_v1 import ALGO_VERSION, farming_frozen
from league.domain.ratings_algo import MatchInput, PlayerChange, PlayerState
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
from league.services import awards as award_service
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
from league.services.ratings import (
    algo_of,
    awards_by_match,
    initial_state,
    params_for,
    run_params,
)

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Totals:
    """What one (match, player) pair adds up to in the ledger, over every run."""

    delta: int = 0
    mu_delta: Decimal = ZERO
    sigma_delta: Decimal = ZERO
    award_adj: Decimal = ZERO

    def __sub__(self, other: Totals) -> Totals:
        return Totals(
            self.delta - other.delta,
            self.mu_delta - other.mu_delta,
            self.sigma_delta - other.sigma_delta,
            self.award_adj - other.award_adj,
        )

    def __bool__(self) -> bool:
        return bool(self.delta or self.mu_delta or self.sigma_delta or self.award_adj)


def _totals(change: PlayerChange) -> Totals:
    return Totals(
        change.delta,
        change.mu_delta or ZERO,
        change.sigma_delta or ZERO,
        change.award_adj or ZERO,
    )


@dataclass(slots=True)
class Fold:
    """What the ledger should say, given the current facts."""

    events: dict[tuple[int, int], PlayerChange] = field(default_factory=dict)
    states: dict[int, PlayerState] = field(default_factory=dict)
    games: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    wins: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    losses: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    last_played: dict[int, datetime | None] = field(default_factory=dict)
    # Team A's chance before each rated match, and the outcome, for calibration.
    expected_a: dict[int, float] = field(default_factory=dict)
    outcomes: dict[int, str] = field(default_factory=dict)
    # MVP awards counted so far, and whose award share was frozen in which match.
    mvps: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    frozen: dict[int, frozenset[int]] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)  # Rated matches, in timeline order.

    @property
    def ratings(self) -> dict[int, int]:
        return {player_id: state.rating for player_id, state in self.states.items()}


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


def _adjusted(state: PlayerState, event: RatingEvent) -> PlayerState:
    return PlayerState(
        rating=state.rating + event.delta,
        mu=state.mu + event.mu_delta,
        sigma=state.sigma + event.sigma_delta,
        games=state.games,
        last_played_at=state.last_played_at,
    )


def fold_season(
    season: Season,
    *,
    outcome_override: dict[int, str] | None = None,
    roster_override: dict[int, dict[int, str]] | None = None,
    awards_override: Mapping[int, Mapping[int, str]] | None = None,
    params_override: Mapping[str, object] | None = None,
) -> Fold:
    """Replay every rated match and manual adjustment in order, from the start state.

    The overrides make this usable for previews and calibration before any write.
    """
    outcomes = {**_latest_outcomes(season), **(outcome_override or {})}
    rosters = {**_rosters(season), **(roster_override or {})}
    algo = algo_of(season)
    params = {**params_for(season), **(params_override or {})}

    timeline: list[tuple[datetime, str, int, Match | RatingEvent]] = []
    rated: list[int] = []
    for match in Match.objects.filter(season=season, ended_at__isnull=False).only("id", "ended_at"):
        if outcomes.get(match.pk) in {"A", "B"}:
            timeline.append((cast(datetime, match.ended_at), "match", match.pk, match))
            rated.append(match.pk)
    adjustments = RatingEvent.objects.filter(
        season=season, run__kind__in=(RunKind.SEED, RunKind.ADMIN)
    ).select_related("run")
    for event in adjustments:
        timeline.append((event.created_at, "adjust", event.pk, event))
    timeline.sort(key=lambda entry: entry[:3])
    awards = {**awards_by_match(rated), **(awards_override or {})}

    fold = Fold()
    start = algo.initial(params)

    def state(player_id: int) -> PlayerState:
        return fold.states.get(player_id, start)

    for _at, _kind, _pk, item in timeline:
        if isinstance(item, RatingEvent):
            fold.states[item.player_id] = _adjusted(state(item.player_id), item)
            continue
        match_roster = rosters.get(item.pk, {})
        team_a = tuple(sorted(p for p, team in match_roster.items() if team == "A"))
        team_b = tuple(sorted(p for p, team in match_roster.items() if team == "B"))
        if not team_a or not team_b:
            continue
        outcome = outcomes[item.pk]
        states = {p: state(p) for p in (*team_a, *team_b)}
        match_awards = awards.get(item.pk, {})
        history = {p: (states[p].games, fold.mvps[p]) for p in match_awards if p in states}
        frozen = farming_frozen(match_awards, history, params)
        changes = algo.rate(
            states,
            MatchInput(team_a, team_b, outcome, match_awards, item.ended_at, frozen),
            params,
        )
        fold.expected_a[item.pk] = changes[team_a[0]].expected
        fold.outcomes[item.pk] = outcome
        fold.order.append(item.pk)
        if frozen:
            fold.frozen[item.pk] = frozen
        for player_id, kind in match_awards.items():
            if kind == "mvp" and player_id in match_roster:
                fold.mvps[player_id] += 1
        for player_id in (*team_a, *team_b):
            change = changes[player_id]
            fold.states[player_id] = change.applied_to(states[player_id], item.ended_at)
            fold.events[(item.pk, player_id)] = change
            fold.games[player_id] += 1
            won = (match_roster[player_id] == "A") == (outcome == "A")
            fold.wins[player_id] += 1 if won else 0
            fold.losses[player_id] += 0 if won else 1
            fold.last_played[player_id] = item.ended_at
    return fold


def _ledger_totals(season: Season) -> dict[tuple[int, int], Totals]:
    rows = (
        RatingEvent.objects.filter(season=season, match__isnull=False)
        .values("match_id", "player_id")
        .annotate(
            total=Sum("delta"),
            mu=Sum("mu_delta"),
            sigma=Sum("sigma_delta"),
            award=Sum("award_adj"),
        )
        .values_list("match_id", "player_id", "total", "mu", "sigma", "award")
    )
    return {
        (match_id, player_id): Totals(total, mu or ZERO, sigma or ZERO, award or ZERO)
        for match_id, player_id, total, mu, sigma, award in rows
    }


def diff_against_ledger(season: Season, fold: Fold) -> dict[tuple[int, int], Totals]:
    """(match, player) to what the ledger still needs, to agree with the fold."""
    ledger = _ledger_totals(season)
    diffs: dict[tuple[int, int], Totals] = {}
    for key in set(ledger) | set(fold.events):
        target = _totals(fold.events[key]) if key in fold.events else Totals()
        needed = target - ledger.get(key, Totals())
        if needed:
            diffs[key] = needed
    return diffs


@dataclass(frozen=True, slots=True)
class Preview:
    """Net rating change per player, and how many ledger rows the correction would write."""

    net_by_player: dict[int, int]
    events_to_write: int


def _preview(diffs: dict[tuple[int, int], Totals]) -> Preview:
    net: dict[int, int] = defaultdict(int)
    for (_match_id, player_id), needed in diffs.items():
        net[player_id] += needed.delta
    return Preview(net_by_player={p: d for p, d in net.items() if d}, events_to_write=len(diffs))


def preview_outcome(match: Match, outcome: str) -> Preview:
    """What changing this match's result would do. Writes nothing."""
    if outcome not in {"A", "B", "void"}:
        raise InvalidAction("Outcome must be A, B or void.")
    fold = fold_season(match.season, outcome_override={match.pk: outcome})
    return _preview(diff_against_ledger(match.season, fold))


def preview_sub(match: Match, out_player: Player, in_player: Player) -> Preview:
    current = {p.player_id: p.team for p in roster(match)}
    if out_player.pk not in current:
        raise InvalidAction(f"{out_player.display_name} is not on this match's roster.")
    if in_player.pk in current:
        raise InvalidAction(f"{in_player.display_name} is already on this match's roster.")
    current[in_player.pk] = current.pop(out_player.pk)
    fold = fold_season(match.season, roster_override={match.pk: current})
    return _preview(diff_against_ledger(match.season, fold))


def preview_awards(match: Match, awards: Mapping[int, str]) -> Preview:
    """What setting these awards would do to ratings. Writes nothing."""
    award_service.validate(match, awards)
    fold = fold_season(match.season, awards_override={match.pk: awards})
    return _preview(diff_against_ledger(match.season, fold))


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
            algo_version=ALGO_VERSION,
            params=run_params(season),
            actor=actor,
            reason=reason[:500],
            created_at=created_at,
        )
        start = initial_state(season)
        running: dict[int, tuple[int, Decimal, Decimal]] = {
            row.player_id: (row.rating, row.mu, row.sigma)
            for row in PlayerRating.objects.filter(season=season)
        }
        events = []
        for (match_id, player_id), needed in sorted(diffs.items()):
            rating, mu, sigma = running.get(player_id, (start.rating, start.mu, start.sigma))
            rating += needed.delta
            mu += needed.mu_delta
            sigma += needed.sigma_delta
            running[player_id] = (rating, mu, sigma)
            target = fold.events.get((match_id, player_id))
            events.append(
                RatingEvent(
                    run=run,
                    season=season,
                    player_id=player_id,
                    match_id=match_id,
                    delta=needed.delta,
                    rating_after=rating,
                    expected=target.expected if target else None,
                    team_avg=target.team_avg if target else None,
                    opp_avg=target.opp_avg if target else None,
                    mu_delta=needed.mu_delta,
                    sigma_delta=needed.sigma_delta,
                    mu_after=mu,
                    sigma_after=sigma,
                    award_adj=needed.award_adj or None,
                    cap_applied=bool(target and target.cap_applied),
                    created_at=created_at,
                )
            )
        RatingEvent.objects.bulk_create(events)
    rebuild_projection(season, fold)
    return run


def rebuild_projection(season: Season, fold: Fold | None = None) -> int:
    """Make `player_ratings` agree with the ledger and the facts. Returns rows touched."""
    fold = fold or fold_season(season)
    start = initial_state(season)
    ledger = (
        RatingEvent.objects.filter(season=season)
        .values("player_id")
        .annotate(total=Sum("delta"), mu=Sum("mu_delta"), sigma=Sum("sigma_delta"))
        .values_list("player_id", "total", "mu", "sigma")
    )
    touched = 0
    for player_id, total, mu, sigma in ledger:
        PlayerRating.objects.update_or_create(
            season=season,
            player_id=player_id,
            defaults={
                "rating": season.start_rating + total,
                "mu": start.mu + (mu or ZERO),
                "sigma": start.sigma + (sigma or ZERO),
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


@transaction.atomic
def apply_award_correction(
    match_id: int,
    awards: Mapping[int, str],
    source: str,
    actor: Player | None,
    reason: str,
) -> RatingRun | None:
    """Set a match's MVP and key players. Before the result is final this only records them, and
    finalising rates with them. After, the season is refolded and the differences appended."""
    match = lock_match(match_id)
    lock_community(match.community_id)
    revision = award_service.record(match, awards, source, actor)
    if revision is None or match.status != Status.COMPLETED:
        return None
    run = _write_correction(match.season, match, f"awards:{revision}", actor, reason)
    add_effect(
        match.community,
        "refresh_results",
        match=match,
        dedupe_key=f"refresh_results:{match.pk}:{revision}",
    )
    return run
