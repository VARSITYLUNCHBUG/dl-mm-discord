"""Voiding a game someone left in its first minutes.

A captain reports it while the game is on. The other captain agrees, and the match is voided with
no rating change: the leaver gets a leaver penalty and stays out of the queue, everyone else goes
back in. If the other captain says no, or doesn't answer, staff decide (`/match early_leave`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction

from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.models import EarlyLeave, Match, MatchParticipant, Player
from league.models.matches import EarlyLeaveStatus, Source
from league.models.moderation import PenaltyKind
from league.services import penalties
from league.services.common import (
    add_effect,
    bump,
    lock_match,
    mode_of,
    now,
    render_board,
    roster,
)


def game_started_at(match: Match) -> datetime | None:
    return match.api_started_at or match.started_at


def pending(match: Match) -> EarlyLeave | None:
    return (
        EarlyLeave.objects.filter(match=match, status=EarlyLeaveStatus.PENDING)
        .select_related("leaver", "requested_by")
        .first()
    )


def _captain(match: Match, player: Player) -> MatchParticipant:
    row = match.participants.filter(player=player, is_captain=True, is_active=True).first()
    if row is None:
        raise NotAllowed("Only a captain can do that.")
    return row


def can_report(match: Match) -> bool:
    started = game_started_at(match)
    if match.status != Status.IN_PROGRESS or started is None:
        return False
    window = timedelta(minutes=mode_of(match).results.early_leave_report_minutes)
    return now() <= started + window


@transaction.atomic
def report(match_id: int, captain: Player, leaver_id: int) -> EarlyLeave:
    match = lock_match(match_id)
    if match.status != Status.IN_PROGRESS:
        raise StaleAction("The game isn't on.")
    _captain(match, captain)
    if not can_report(match):
        raise InvalidAction("It's too late to report an early leave. Ask staff if it matters.")
    if pending(match) is not None:
        raise InvalidAction("An early leave is already waiting for an answer.")
    leaver = roster(match).filter(player_id=leaver_id).select_related("player").first()
    if leaver is None:
        raise InvalidAction("That player isn't in this match.")
    results = mode_of(match).results
    claim = EarlyLeave.objects.create(
        match=match,
        leaver=leaver.player,
        requested_by=captain,
        deadline_at=now() + timedelta(seconds=results.early_leave_answer_seconds),
        created_at=now(),
    )
    other = (
        match.participants.filter(is_captain=True, is_active=True)
        .exclude(player=captain)
        .select_related("player")
        .first()
    )
    if other is not None:
        add_effect(
            match.community,
            "ping",
            {
                "user_ids": [other.player.discord_user_id],
                "text": f"{captain.display_name} says **{leaver.player.display_name}** left in "
                f"the first {results.early_leave_minutes} minutes. Agree on the board to void "
                "the match.",
            },
            match=match,
        )
    bump(match)
    render_board(match)
    return claim


@transaction.atomic
def answer(match_id: int, captain: Player, agree: bool) -> None:
    match = lock_match(match_id)
    claim = pending(match)
    if claim is None or match.status != Status.IN_PROGRESS:
        raise StaleAction("There's no early leave to answer.")
    _captain(match, captain)
    if captain.pk == claim.requested_by_id:
        raise NotAllowed("The other captain has to answer this.")
    if agree:
        _void(match, claim, captain, Source.USER)
        return
    _close(claim, EarlyLeaveStatus.REJECTED, captain)
    add_effect(
        match.community,
        "ping_staff",
        {
            "text": f"needs a decision: did {claim.leaver.display_name} leave early? The captains "
            "disagree. `/match early_leave` voids it."
        },
        match=match,
    )
    bump(match)
    render_board(match)


@transaction.atomic
def staff_void(match_id: int, leaver: Player, actor: Player | None) -> None:
    """Staff deciding it was an early leave, with or without a pending report."""
    match = lock_match(match_id)
    if match.status not in {Status.IN_PROGRESS, Status.AWAITING_CONFIRMATION, Status.DISPUTED}:
        raise InvalidAction("Only a match being played or awaiting its result can be voided so.")
    if not roster(match).filter(player=leaver).exists():
        raise InvalidAction(f"{leaver.display_name} isn't in this match.")
    claim = pending(match) or EarlyLeave.objects.create(
        match=match,
        leaver=leaver,
        requested_by=actor or leaver,
        deadline_at=now(),
        created_at=now(),
    )
    claim.leaver = leaver
    _void(match, claim, actor, Source.STAFF)


def _close(claim: EarlyLeave, status: str, actor: Player | None) -> None:
    claim.status = status
    claim.resolved_by = actor
    claim.resolved_at = now()
    claim.save(update_fields=["status", "leaver", "resolved_by", "resolved_at"])


def _void(match: Match, claim: EarlyLeave, actor: Player | None, source: str) -> None:
    from league.services.lobby import void_live

    _close(claim, EarlyLeaveStatus.APPROVED, actor)
    match.participants.filter(player=claim.leaver).update(left_early=True)
    penalties.issue(
        match.community,
        claim.leaver,
        PenaltyKind.LEAVER,
        match=match,
        reason=f"Left match #{match.pk} in its first minutes",
    )
    void_live(
        match,
        source=source,
        actor=actor,
        reason=f"{claim.leaver.display_name} left in the first minutes",
        requeue=True,
        skip_requeue={claim.leaver_id},
    )


def tick(moment: datetime) -> int:
    """Unanswered reports go to staff."""
    fired = 0
    due = EarlyLeave.objects.filter(status=EarlyLeaveStatus.PENDING, deadline_at__lte=moment)
    for claim_id in due.values_list("pk", flat=True):
        with transaction.atomic():
            claim = EarlyLeave.objects.select_related("leaver").get(pk=claim_id)
            match = lock_match(claim.match_id)
            claim.refresh_from_db()
            if claim.status != EarlyLeaveStatus.PENDING:
                continue
            _close(claim, EarlyLeaveStatus.EXPIRED, None)
            add_effect(
                match.community,
                "ping_staff",
                {
                    "text": f"needs a decision: {claim.leaver.display_name} may have left "
                    "early and the other captain didn't answer. `/match early_leave` voids it."
                },
                match=match,
            )
            bump(match)
            render_board(match)
            fired += 1
    return fired
