"""Reporting and finalising. One screenshot finalises; the other captain can confirm or dispute."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.models import Match, MatchResult, Player, ResultSubmission
from league.models.matches import Source
from league.models.results import Verdict
from league.services import ratings
from league.services.boards import outcome_label
from league.services.common import (
    add_effect,
    clear_notices,
    close_subs,
    lock_community,
    lock_match,
    mode_of,
    now,
    render_board,
    render_queue,
    save_transition,
    seconds_from_now,
    transition,
)
from league.services.lobby import next_revision, schedule_cleanup, void_live
from league.services.queue import try_pop

STAFF_REPING_SECONDS = 6 * 3600


@dataclass(frozen=True, slots=True)
class ImageRef:
    """A screenshot already copied to the archive channel."""

    sha256: str
    archive_channel_id: int
    archive_message_id: int
    attachment_id: int


def _captain_team(match: Match, player: Player) -> str:
    participant = match.participants.filter(player=player, is_captain=True, is_active=True).first()
    if participant is None:
        raise NotAllowed("Only a captain can do that.")
    return participant.team


def check_can_report(match_id: int, player: Player) -> None:
    """Fast check before opening the modal. The submission checks again under the lock."""
    match = Match.objects.filter(pk=match_id).first()
    if match is None or match.status not in {Status.IN_PROGRESS, Status.AWAITING_CONFIRMATION}:
        raise StaleAction("This match isn't waiting for a result.")
    _captain_team(match, player)
    if match.submissions.filter(captain=player).exists():
        raise InvalidAction("You've already responded for this match.")


def _ping_staff(match: Match, text: str) -> None:
    add_effect(match.community, "ping_staff", {"text": text}, match=match)


def _create_submission(
    match: Match,
    captain: Player,
    verdict: str,
    *,
    winner: str = "",
    image: ImageRef | None = None,
    deadlock_match_id: int | None = None,
    note: str = "",
) -> ResultSubmission:
    return ResultSubmission.objects.create(
        match=match,
        captain=captain,
        verdict=verdict,
        claimed_winner=winner,
        deadlock_match_id=deadlock_match_id,
        image_sha256=image.sha256 if image else "",
        archive_channel_id=image.archive_channel_id if image else None,
        archive_message_id=image.archive_message_id if image else None,
        attachment_id=image.attachment_id if image else None,
        note=note[:500],
        created_at=now(),
    )


def _first_report(match: Match) -> ResultSubmission | None:
    return match.submissions.filter(verdict=Verdict.REPORT).order_by("id").first()


@transaction.atomic
def submit_report(
    match_id: int,
    captain: Player,
    winner: str,
    image: ImageRef,
    deadlock_match_id: int | None = None,
) -> str:
    """Record a captain's report and return the match's new status."""
    match = lock_match(match_id)
    if match.status not in {Status.IN_PROGRESS, Status.AWAITING_CONFIRMATION}:
        raise StaleAction("This match isn't waiting for a result.")
    if winner not in {"A", "B"}:
        raise InvalidAction("Choose the winning team.")
    _captain_team(match, captain)
    if match.submissions.filter(captain=captain).exists():
        raise InvalidAction("You've already responded for this match.")

    if match.status == Status.IN_PROGRESS:
        close_subs(match)  # The game is over, so nobody needs to come in now.
        _create_submission(
            match,
            captain,
            Verdict.REPORT,
            winner=winner,
            image=image,
            deadlock_match_id=deadlock_match_id,
        )
        mode = mode_of(match)
        transition(
            match,
            Status.AWAITING_CONFIRMATION,
            source=Source.USER,
            actor=captain,
            deadline_at=seconds_from_now(mode.results.confirm_window_seconds),
        )
        save_transition(match)
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
                    "text": f"{captain.display_name} reported {outcome_label(match, winner)} "
                    "won. Confirm or dispute on the board. Silence confirms when the timer "
                    "runs out.",
                },
                match=match,
            )
        render_board(match)
        return match.status

    first = _first_report(match)
    if first is None:  # Unreachable through the bot, but never finalise on no report.
        raise InvalidAction("There's no report to respond to.")
    if first.claimed_winner == winner:
        _create_submission(
            match,
            captain,
            Verdict.CONFIRM,
            winner=winner,
            image=image,
            deadlock_match_id=deadlock_match_id,
        )
        _finalise(match, winner, source=Source.USER, actor=captain)
    else:
        _create_submission(
            match,
            captain,
            Verdict.DISPUTE,
            winner=winner,
            image=image,
            deadlock_match_id=deadlock_match_id,
            note="Reported the other team as the winner.",
        )
        _dispute(match, captain, "The captains reported different winners.")
    return match.status


@transaction.atomic
def confirm(match_id: int, captain: Player) -> None:
    match = lock_match(match_id)
    if match.status != Status.AWAITING_CONFIRMATION:
        raise StaleAction("There's nothing to confirm.")
    _captain_team(match, captain)
    if match.submissions.filter(captain=captain).exists():
        raise InvalidAction("You've already responded for this match.")
    first = _first_report(match)
    if first is None:
        raise InvalidAction("There's no report to confirm.")
    _create_submission(match, captain, Verdict.CONFIRM, winner=first.claimed_winner)
    _finalise(match, first.claimed_winner, source=Source.USER, actor=captain)


@transaction.atomic
def dispute(match_id: int, captain: Player, note: str) -> None:
    match = lock_match(match_id)
    if match.status != Status.AWAITING_CONFIRMATION:
        raise StaleAction("There's nothing to dispute.")
    _captain_team(match, captain)
    if match.submissions.filter(captain=captain).exists():
        raise InvalidAction("You've already responded for this match.")
    _create_submission(match, captain, Verdict.DISPUTE, note=note)
    _dispute(match, captain, note or "A captain disputed the result.")


def _dispute(match: Match, actor: Player | None, reason: str) -> None:
    source = Source.USER if actor else Source.TIMEOUT
    transition(
        match,
        Status.DISPUTED,
        source=source,
        actor=actor,
        deadline_at=seconds_from_now(STAFF_REPING_SECONDS),
    )
    save_transition(match)
    _ping_staff(match, f"needs a decision: {reason}")
    render_board(match)


@transaction.atomic
def resolve_dispute(match_id: int, outcome: str, actor: Player | None, reason: str = "") -> None:
    match = lock_match(match_id)
    if match.status != Status.DISPUTED:
        raise StaleAction("That match isn't disputed any more.")
    if outcome == "void":
        void_live(match, source=Source.STAFF, actor=actor, reason=reason or "Voided by staff")
    elif outcome in {"A", "B"}:
        _finalise(match, outcome, source=Source.STAFF, actor=actor, reason=reason)
    else:
        raise InvalidAction("Outcome must be A, B or void.")


def on_confirm_timeout(match: Match) -> None:
    first = _first_report(match)
    if first is None:
        _dispute(match, None, "A confirmation window closed with no report on file.")
        return
    _finalise(match, first.claimed_winner, source=Source.TIMEOUT, actor=None)


def on_in_progress_timeout(match: Match) -> None:
    _dispute(match, None, "No captain has reported a result.")


def on_disputed_timeout(match: Match) -> None:
    match.deadline_at = seconds_from_now(STAFF_REPING_SECONDS)
    match.version += 1
    match.save(update_fields=["deadline_at", "version"])
    clear_notices(match)  # One open request for staff, however long it waits.
    _ping_staff(match, "still needs a decision.")


def _finalise(
    match: Match, outcome: str, *, source: str, actor: Player | None, reason: str = ""
) -> None:
    """Result, ledger and projections commit together. Discord effects follow the commit."""
    community = lock_community(match.community_id)  # Keeps ledger order equal to `ended_at` order.
    moment = now()
    revision = next_revision(match)
    MatchResult.objects.create(
        match=match,
        revision=revision,
        outcome=outcome,
        source=source,
        actor=actor,
        reason=reason[:500],
        created_at=moment,
    )
    match.winner = outcome
    match.ended_at = moment
    transition(match, Status.COMPLETED, source=source, actor=actor)
    save_transition(match, "winner", "ended_at")
    ratings.apply_live_result(match, outcome, revision)
    match.participants.filter(is_active=True).update(is_active=False)
    add_effect(community, "post_results", match=match, dedupe_key=f"post_results:{match.pk}")
    schedule_cleanup(match)
    render_board(match)
    render_queue(community)
    try_pop(community)
