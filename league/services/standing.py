"""Strikes and captain eligibility, folded from facts each time they are needed.

Nothing here is stored as a running counter. Offences are penalties, clean games are completed
matches and staff decisions are their own append-only rows, so a void, a waiver or a late sub
correction changes the answer the next time it is asked.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Count

from league.domain.engine import Status
from league.domain.errors import InvalidAction
from league.domain.mode_schema import DraftMode
from league.domain.standing import (
    CLEAN,
    CaptainRule,
    Eligibility,
    Standing,
    StrikeEntry,
    StrikePolicy,
    captain_eligibility,
    fold_strikes,
)
from league.models import (
    CaptainOverride,
    Community,
    MatchParticipant,
    Penalty,
    Player,
    StrikeAdjustment,
)
from league.models.community import effective_penalty_policy
from league.models.moderation import CaptainStatus
from league.services.common import now


def policy_of(community: Community) -> dict[str, object]:
    return effective_penalty_policy(community.penalty_policy)


def strike_policy(community: Community) -> StrikePolicy:
    return StrikePolicy.from_policy(policy_of(community))


def completed_matches(community: Community, player_ids: Iterable[int]) -> dict[int, int]:
    """Completed matches each player finished on the roster, across every season."""
    ids = list(player_ids)
    rows = (
        MatchParticipant.objects.filter(
            player_id__in=ids,
            match__community=community,
            match__status=Status.COMPLETED,
            removed_reason="",
        )
        .exclude(team="")
        .values("player_id")
        .annotate(total=Count("id"))
        .values_list("player_id", "total")
    )
    counts = dict.fromkeys(ids, 0)
    counts.update(dict(rows))
    return counts


def _entries(community: Community, player_ids: list[int]) -> dict[int, list[StrikeEntry]]:
    entries: dict[int, list[StrikeEntry]] = defaultdict(list)
    penalised: set[tuple[int, int]] = set()
    penalties = Penalty.objects.filter(community=community, player_id__in=player_ids)
    for row in penalties.values_list(
        "pk", "player_id", "starts_at", "strikes", "strikes_waived", "match_id"
    ):
        pk, player_id, starts_at, strikes, waived, match_id = row
        if match_id is not None:
            penalised.add((player_id, match_id))
        if not waived and strikes:
            entries[player_id].append(StrikeEntry(starts_at, "offence", strikes, pk))
    clean = MatchParticipant.objects.filter(
        player_id__in=player_ids,
        match__community=community,
        match__status=Status.COMPLETED,
        match__ended_at__isnull=False,
        removed_reason="",
    ).exclude(team="")
    for player_id, match_id, ended_at in clean.values_list(
        "player_id", "match_id", "match__ended_at"
    ):
        if ended_at is not None and (player_id, match_id) not in penalised:
            entries[player_id].append(StrikeEntry(ended_at, "clean", 0, match_id))
    adjustments = StrikeAdjustment.objects.filter(community=community, player_id__in=player_ids)
    for pk, player_id, created_at, delta in adjustments.values_list(
        "pk", "player_id", "created_at", "delta"
    ):
        entries[player_id].append(StrikeEntry(created_at, "adjust", delta, pk))
    return entries


def standings(
    community: Community, player_ids: Iterable[int], at: datetime | None = None
) -> dict[int, Standing]:
    ids = list(player_ids)
    if not ids:
        return {}
    policy = strike_policy(community)
    moment = at or now()
    entries = _entries(community, ids)
    return {
        player_id: fold_strikes(entries[player_id], policy, moment)
        if entries.get(player_id)
        else CLEAN
        for player_id in ids
    }


def standing(community: Community, player: Player, at: datetime | None = None) -> Standing:
    return standings(community, [player.pk], at)[player.pk]


def overrides(community: Community, player_ids: Iterable[int]) -> dict[int, str]:
    """The latest staff decision per player: "allow", "bar" or "" for the normal rule."""
    latest: dict[int, str] = {}
    rows = CaptainOverride.objects.filter(community=community, player_id__in=list(player_ids))
    for player_id, status in rows.order_by("id").values_list("player_id", "status"):
        latest[player_id] = "" if status == CaptainStatus.CLEAR else status
    return latest


def captain_rule(mode: DraftMode) -> CaptainRule:
    return CaptainRule(min_games=mode.captains.min_games, max_strikes=mode.captains.max_strikes)


@dataclass(frozen=True, slots=True)
class PlayerStanding:
    standing: Standing
    eligibility: Eligibility


def eligibility(
    community: Community, mode: DraftMode, player_ids: Iterable[int]
) -> dict[int, PlayerStanding]:
    """Captain eligibility for each player, under the match's own mode rule."""
    ids = list(player_ids)
    rule = captain_rule(mode)
    games = completed_matches(community, ids)
    strikes = standings(community, ids)
    staff = overrides(community, ids)
    return {
        player_id: PlayerStanding(
            standing=strikes[player_id],
            eligibility=captain_eligibility(
                games[player_id], strikes[player_id].strikes, staff.get(player_id, ""), rule
            ),
        )
        for player_id in ids
    }


def set_override(
    community: Community, player: Player, status: str, actor: Player | None, reason: str
) -> CaptainOverride:
    if status not in CaptainStatus.values:
        raise ValueError(status)
    return CaptainOverride.objects.create(
        community=community,
        player=player,
        status=status,
        actor=actor,
        reason=reason[:500],
        created_at=now(),
    )


def adjust_strikes(
    community: Community, player: Player, delta: int, actor: Player | None, reason: str
) -> Standing:
    if not delta:
        raise InvalidAction("The change can't be zero.")
    if not reason.strip():
        raise InvalidAction("A strike change needs a reason.")
    StrikeAdjustment.objects.create(
        community=community,
        player=player,
        delta=delta,
        actor=actor,
        reason=reason[:500],
        created_at=now(),
    )
    return standing(community, player)


def describe(standing_: Standing, policy: StrikePolicy) -> str:
    """One line for a player: "2 strikes: 1 more clean match removes one"."""
    if standing_.clean:
        return "No strikes."
    games = standing_.games_to_next or policy.games_per_strike
    noun = "strike" if standing_.strikes == 1 else "strikes"
    match_word, verb = ("match", "removes") if games == 1 else ("matches", "remove")
    text = f"{standing_.strikes} {noun}: {games} more clean {match_word} {verb} one"
    if standing_.next_decay_at is not None:
        text += f", or wait until <t:{int(standing_.next_decay_at.timestamp())}:d>"
    return text + "."
