"""Lobby lifecycle before the draft: creation, ready check, the captain vote, voids, subs."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Collection, Sequence

from django.db import transaction
from django.db.models import Max, Q

from league.domain.engine import (
    LIVE,
    TERMINAL,
    CaptainCandidate,
    Status,
    first_pick_team,
    tally_captain_votes,
)
from league.domain.errors import InvalidAction, StaleAction
from league.domain.mode_schema import parse_mode
from league.models import (
    CaptainVote,
    Champion,
    Community,
    Match,
    MatchParticipant,
    MatchResult,
    MatchTransition,
    Player,
    QueueEntry,
)
from league.models.matches import RemovedReason, Source
from league.models.moderation import PenaltyKind
from league.services import penalties, prio
from league.services.common import (
    active_participants,
    add_effect,
    bump,
    captain_of,
    close_subs,
    lock_community,
    lock_match,
    mode_of,
    now,
    on_hold,
    open_season,
    rating_of,
    render_board,
    render_queue,
    require_not_on_hold,
    require_participant,
    save_transition,
    seconds_from_now,
    transition,
)

logger = logging.getLogger(__name__)

# Minutes the lobby text channel stays after the match ends, for last words and disputes.
TEXT_CHANNEL_GRACE_MINUTES = 15


def create_match(community: Community, entries: Sequence[QueueEntry]) -> Match | None:
    """Turn the front of the queue into a lobby. Caller holds the community lock."""
    mode = parse_mode(community.active_mode.body)
    excluded = set(mode.champion_draft.excluded_champion_ids)
    pool = sorted(
        pk
        for pk in Champion.objects.filter(is_enabled=True).values_list("pk", flat=True)
        if pk not in excluded
    )
    if len(pool) < mode.champions_consumed_max:
        logger.error(
            "Queue full but champion pool too small",
            extra={"pool": len(pool), "needed": mode.champions_consumed_max},
        )
        return None

    moment = now()
    match = Match.objects.create(
        community=community,
        season=open_season(community),
        mode_version=community.active_mode,
        status=Status.READY_CHECK,
        deadline_at=seconds_from_now(mode.ready_check.seconds),
        rng_seed=secrets.randbits(62),
        champion_pool=pool,
        created_at=moment,
    )
    MatchTransition.objects.create(
        match=match, to_status=Status.READY_CHECK, source=Source.SYSTEM, created_at=moment
    )
    MatchParticipant.objects.bulk_create(
        MatchParticipant(match=match, player=entry.player, queue_joined_at=entry.joined_at)
        for entry in entries
    )
    QueueEntry.objects.filter(pk__in=[entry.pk for entry in entries]).delete()
    prio.drop_players(community.pk, [entry.player.discord_user_id for entry in entries])

    add_effect(community, "create_lobby", match=match, dedupe_key=f"create_lobby:{match.pk}")
    for entry in entries:
        _gather(match, entry.player)
    render_queue(community)
    logger.info("Lobby created", extra={"match": match.pk})
    return match


def _gather(match: Match, player: Player, *, sub: bool = False) -> None:
    """Get them into voice. A sub joins mid-match, so they go to their team's channel if it's up."""
    add_effect(
        match.community,
        "gather_player",
        {"player_id": player.pk, "discord_user_id": player.discord_user_id, "sub": sub},
        match=match,
        dedupe_key=f"gather:{match.pk}:{player.pk}",
    )


# --- Ready check -----------------------------------------------------------------------------


@transaction.atomic
def mark_ready(match_id: int, player: Player) -> None:
    match = lock_match(match_id)
    if match.status != Status.READY_CHECK:
        raise StaleAction("The ready check has already finished.")
    require_not_on_hold(match)
    participant = require_participant(match, player)
    if participant.ready_at is not None:
        raise InvalidAction("You're already marked as ready.")
    participant.ready_at = now()
    participant.save(update_fields=["ready_at"])
    bump(match)
    if active_participants(match).filter(ready_at__isnull=True).exists():
        render_board(match)
    else:
        begin_captain_phase(match)


def on_ready_timeout(match: Match) -> None:
    """Replace everyone who missed it from the queue, or cancel if the queue can't cover them."""
    community = lock_community(match.community_id)
    mode = mode_of(match)
    missed = list(active_participants(match).filter(ready_at__isnull=True))
    if not missed:
        begin_captain_phase(match)
        return

    moment = now()
    replacements = list(
        QueueEntry.objects.filter(community=community).select_related("player")[: len(missed)]
    )
    for participant in missed:
        participant.is_active = False
        participant.removed_reason = RemovedReason.MISSED_READY
        participant.removed_at = moment
        participant.save(update_fields=["is_active", "removed_reason", "removed_at"])
        penalties.issue(
            community,
            participant.player,
            PenaltyKind.MISSED_READY,
            match=match,
            reason=f"Missed the ready check for match #{match.pk}",
        )

    if len(replacements) < len(missed):
        void_live(match, source=Source.TIMEOUT, reason="Not enough players ready", requeue=True)
        return

    for participant in missed:
        add_effect(
            community,
            "remove_player",
            {"discord_user_id": participant.player.discord_user_id},
            match=match,
        )
    for entry in replacements:
        MatchParticipant.objects.create(
            match=match, player=entry.player, queue_joined_at=entry.joined_at
        )
        add_effect(
            community,
            "grant_access",
            {"discord_user_id": entry.player.discord_user_id},
            match=match,
        )
        _gather(match, entry.player)
    QueueEntry.objects.filter(pk__in=[entry.pk for entry in replacements]).delete()

    match.deadline_at = seconds_from_now(mode.ready_check.seconds)
    bump(match, "deadline_at")
    render_board(match)
    render_queue(community)


# --- Captain vote ----------------------------------------------------------------------------

VOTES_PER_PLAYER = 2


def begin_captain_phase(match: Match) -> None:
    """Everyone is ready: open the captain vote."""
    transition(
        match,
        Status.CAPTAIN_PHASE,
        source=Source.SYSTEM,
        deadline_at=seconds_from_now(mode_of(match).captains.vote_seconds),
    )
    save_transition(match)
    ids = [p.player.discord_user_id for p in active_participants(match)]
    add_effect(
        match.community,
        "ping",
        {"user_ids": ids, "text": "everyone's ready. Vote for up to two captains on the board."},
        match=match,
    )
    render_board(match)


@transaction.atomic
def cast_captain_votes(match_id: int, player: Player, candidate_ids: Sequence[int]) -> list[Player]:
    """Replace this player's captain votes. Returns who they voted for."""
    match = lock_match(match_id)
    if match.status != Status.CAPTAIN_PHASE:
        raise StaleAction("The captain vote has finished.")
    require_not_on_hold(match)
    require_participant(match, player)
    chosen = list(dict.fromkeys(candidate_ids))
    if not chosen:
        raise InvalidAction("Choose at least one player.")
    if len(chosen) > VOTES_PER_PLAYER:
        raise InvalidAction(f"You can vote for up to {VOTES_PER_PLAYER} players.")
    candidates = {p.player_id: p.player for p in active_participants(match)}
    if any(candidate_id not in candidates for candidate_id in chosen):
        raise InvalidAction("You can only vote for players in this lobby.")
    match.captain_votes.filter(voter=player).delete()
    moment = now()
    CaptainVote.objects.bulk_create(
        CaptainVote(match=match, voter=player, candidate_id=candidate_id, created_at=moment)
        for candidate_id in chosen
    )
    bump(match)
    advance_captain_phase(match)
    return [candidates[candidate_id] for candidate_id in chosen]


def _everyone_voted(match: Match) -> bool:
    voters = set(match.captain_votes.values_list("voter_id", flat=True))
    return all(p.player_id in voters for p in active_participants(match))


def advance_captain_phase(match: Match) -> None:
    """Close the vote once everyone still in the lobby has voted."""
    if on_hold(match) or not _everyone_voted(match):  # A sub picks it up again on arrival.
        render_board(match)
        return
    _close_vote(match, Source.USER)


def on_captain_timeout(match: Match) -> None:
    """Count the votes cast so far. Elo fills any gap."""
    _close_vote(match, Source.TIMEOUT)


def vote_counts(match: Match) -> dict[int, int]:
    """Votes per player, counting only voters and candidates still in the lobby."""
    active = set(active_participants(match).values_list("player_id", flat=True))
    counts: dict[int, int] = {}
    for vote in match.captain_votes.filter(voter_id__in=active, candidate_id__in=active):
        counts[vote.candidate_id] = counts.get(vote.candidate_id, 0) + 1
    return counts


def _close_vote(match: Match, source: str) -> None:
    participants = list(active_participants(match))
    candidates = []
    for participant in participants:
        rating, games = rating_of(match.season, participant.player_id)
        candidates.append(CaptainCandidate(participant.player_id, rating, games))
    counts = vote_counts(match)
    captain_a, captain_b = tally_captain_votes(counts, candidates, match.rng_seed)
    names = {}
    for participant in participants:
        if participant.player_id in {captain_a, captain_b}:
            participant.is_captain = True
            participant.team = "A" if participant.player_id == captain_a else "B"
            participant.save(update_fields=["is_captain", "team"])
            names[participant.player_id] = participant.player.display_name

    def tally(player_id: int) -> str:
        count = counts.get(player_id, 0)
        return f"**{names[player_id]}** ({count} vote{'' if count == 1 else 's'})"

    begin_player_draft(match, source=source)
    # After the transition, which clears earlier notices, so this one stays up.
    add_effect(
        match.community,
        "ping_lobby",
        {"text": f"Captains voted in: {tally(captain_a)} and {tally(captain_b)}."},
        match=match,
    )


def _ping_captains(match: Match, text: str) -> None:
    ids = [p.player.discord_user_id for p in active_participants(match).filter(is_captain=True)]
    add_effect(match.community, "ping", {"user_ids": ids, "text": text}, match=match)


def begin_player_draft(match: Match, source: str) -> None:
    mode = mode_of(match)
    rating_a, _ = rating_of(match.season, captain_of(match, "A").player_id)
    rating_b, _ = rating_of(match.season, captain_of(match, "B").player_id)
    match.player_first_pick = first_pick_team(rating_a, rating_b, match.rng_seed)
    match.step_index = 0
    transition(
        match,
        Status.PLAYER_DRAFT,
        source=source,
        deadline_at=seconds_from_now(mode.player_draft.seconds),
    )
    save_transition(match, "player_first_pick", "step_index")
    first = captain_of(match, match.player_first_pick).player.display_name
    _ping_captains(match, f"the player draft is on. **{first}** picks first.")
    render_board(match)


# --- Voids and substitutes -------------------------------------------------------------------


def next_revision(match: Match) -> int:
    latest = match.results.aggregate(latest=Max("revision"))["latest"]
    return (latest or 0) + 1


def void_live(
    match: Match,
    *,
    source: str,
    reason: str,
    actor: Player | None = None,
    requeue: bool = False,
    skip_requeue: Collection[int] = (),
) -> None:
    """End a live match with no rating change. Caller holds the match lock.

    `skip_requeue` holds player IDs that must not go back in line, such as someone subbing out.
    """
    community = lock_community(match.community_id)
    moment = now()
    close_subs(match)
    MatchResult.objects.create(
        match=match,
        revision=next_revision(match),
        outcome="void",
        source=source,
        actor=actor,
        reason=reason[:500],
        created_at=moment,
    )
    active = list(active_participants(match))
    if requeue:
        for participant in active:
            if participant.ready_at is not None and participant.player_id not in skip_requeue:
                # Keeping the original time puts them ahead of everyone who joined later.
                QueueEntry.objects.get_or_create(
                    community=community,
                    player=participant.player,
                    defaults={"joined_at": participant.queue_joined_at},
                )
    match.participants.filter(is_active=True).update(is_active=False)
    transition(match, Status.VOIDED, source=source, actor=actor)
    match.ended_at = moment
    save_transition(match, "ended_at")
    add_effect(community, "ping_lobby", {"text": f"Match cancelled: {reason}"}, match=match)
    schedule_cleanup(match)
    render_board(match)
    render_queue(community)
    logger.info("Match voided", extra={"match": match.pk, "reason": reason})


def schedule_cleanup(match: Match) -> None:
    community = match.community
    add_effect(community, "cleanup_voice", match=match, dedupe_key=f"cleanup_voice:{match.pk}")
    add_effect(
        community,
        "delete_text_channel",
        match=match,
        dedupe_key=f"delete_text:{match.pk}",
        run_after=seconds_from_now(TEXT_CHANNEL_GRACE_MINUTES * 60),
    )


@transaction.atomic
def staff_void(match_id: int, actor: Player | None, reason: str) -> None:
    match = lock_match(match_id)
    if match.status in TERMINAL:
        raise InvalidAction("That match has finished. Use the correction flow.")
    void_live(match, source=Source.STAFF, actor=actor, reason=reason)


def check_sub_is_free(match: Match, in_player: Player) -> None:
    if MatchParticipant.objects.filter(player=in_player, is_active=True).exists():
        raise InvalidAction(f"{in_player.display_name} is already in a live match.")
    if match.participants.filter(player=in_player).exists():
        raise InvalidAction(f"{in_player.display_name} was already part of this match.")


def swap_player(match: Match, leaving: MatchParticipant, in_player: Player, *, ready: bool) -> None:
    """Put the substitute in the leaving player's place: team, role, draft slot and the result.

    The caller holds the match lock and has checked the substitute is free.
    """
    moment = now()
    community = lock_community(match.community_id)
    QueueEntry.objects.filter(community=community, player=in_player).delete()
    prio.drop_players(community.pk, [in_player.discord_user_id])
    MatchParticipant.objects.create(
        match=match,
        player=in_player,
        queue_joined_at=leaving.queue_joined_at,
        ready_at=moment if ready else None,
        team=leaving.team,
        is_captain=leaving.is_captain,
        draft_pick_number=leaving.draft_pick_number,
    )
    leaving.is_active = False
    leaving.is_captain = False
    leaving.removed_reason = RemovedReason.SUBBED_OUT
    leaving.removed_at = moment
    leaving.save(update_fields=["is_active", "is_captain", "removed_reason", "removed_at"])
    # Their votes, and votes for them, no longer count. The sub gets a vote of their own.
    match.captain_votes.filter(
        Q(voter_id=leaving.player_id) | Q(candidate_id=leaving.player_id)
    ).delete()
    if match.lobby_code_host_id == leaving.player_id:
        match.lobby_code_host = in_player
        bump(match, "lobby_code_host")
    else:
        bump(match)
    add_effect(
        community, "remove_player", {"discord_user_id": leaving.player.discord_user_id}, match=match
    )
    add_effect(
        community, "grant_access", {"discord_user_id": in_player.discord_user_id}, match=match
    )
    _gather(match, in_player, sub=True)
    render_board(match)
    render_queue(community)


@transaction.atomic
def substitute_live(
    match_id: int, out_player: Player, in_player: Player, actor: Player | None
) -> None:
    """Staff swap a player in a live match. The substitute takes their team, role and the result."""
    from league.services import subs  # Avoids a cycle: subs builds on this module.

    match = lock_match(match_id)
    if match.status not in LIVE:
        raise InvalidAction("That match isn't live.")
    leaving = match.participants.filter(player=out_player, is_active=True).first()
    if leaving is None:
        raise InvalidAction(f"{out_player.display_name} isn't active in this match.")
    check_sub_is_free(match, in_player)
    swap_player(match, leaving, in_player, ready=leaving.ready_at is not None)
    subs.after_staff_swap(match, out_player)


def fail_lobby_setup(match_id: int, reason: str) -> None:
    """Discord refused to create the lobby for good: cancel and put players back in line."""
    with transaction.atomic():
        match = lock_match(match_id)
        if match.status in LIVE:
            match.participants.filter(is_active=True, ready_at__isnull=True).update(ready_at=now())
            void_live(match, source=Source.SYSTEM, reason=reason, requeue=True)
