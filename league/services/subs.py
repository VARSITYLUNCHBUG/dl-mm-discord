"""Player-driven substitutes, in two flows.

Sub out opens a slot. A player can ask for themself, which opens it straight away, or a captain
can ask for a teammate, which needs the other captain to agree. From the request until it is
settled the match is on hold: its timer is parked and every action waits.

Sub in fills the slot. Whoever opened it can name someone, who has to accept. If they don't name
anyone in time, or the person they named says no, the slot is offered down the queue in order.
Unfilled by the deadline, the match is cancelled with no rating change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Q

from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.domain.mode_schema import DraftMode
from league.models import (
    CaptainHandoff,
    Match,
    MatchParticipant,
    Player,
    QueueEntry,
    SubOffer,
    SubRequest,
)
from league.models.matches import HandoffStatus, Source
from league.models.moderation import PenaltyKind
from league.models.subs import UNRESOLVED, OfferSource, OfferStatus, SubStatus
from league.services import lobby, penalties
from league.services.common import (
    active_participants,
    add_effect,
    bump,
    get_or_create_player,
    lock_match,
    mode_of,
    now,
    render_board,
    require_participant,
)

logger = logging.getLogger(__name__)

# A sub can come in from the ready check until the game is over. After a result is reported the
# roster is fixed, and changing it is a staff correction.
SUBBABLE = frozenset(
    {
        Status.READY_CHECK,
        Status.CAPTAIN_PHASE,
        Status.PLAYER_DRAFT,
        Status.SIDE_SELECT,
        Status.CHAMPION_DRAFT,
        Status.LOBBY_CODE,
        Status.IN_PROGRESS,
    }
)
# Once the sub is in, the paused timer resumes with at least this long left, so nobody is
# auto-picked the moment the match starts moving again.
RESUME_MIN_SECONDS = 15
# Each pending request adds a line and a row of buttons to the board, which Discord caps at 40
# components. Four fits alongside every phase; past that, staff can sort it out.
MAX_PENDING = 4


def _require_subbable(match: Match) -> None:
    if match.status not in SUBBABLE:
        raise StaleAction("Subs are closed for this match.")


def _game_on(match: Match) -> bool:
    """Leaving once the game is running counts as leaving a game."""
    return match.status == Status.IN_PROGRESS


def _unresolved(match: Match, player_id: int) -> bool:
    return match.sub_requests.filter(leaving_id=player_id, status__in=UNRESOLVED).exists()


def _ping(match: Match, user_ids: list[int], text: str) -> None:
    add_effect(match.community, "ping", {"user_ids": user_ids, "text": text}, match=match)


def _captains(match: Match) -> list[MatchParticipant]:
    return list(active_participants(match).filter(is_captain=True))


def _lock_request(request_id: int) -> tuple[Match, SubRequest]:
    request = SubRequest.objects.filter(pk=request_id).first()
    if request is None:
        raise StaleAction("That sub request no longer exists.")
    match = lock_match(request.match_id)
    request.refresh_from_db()
    return match, request


# --- Sub out ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubOutChoice:
    player_id: int
    name: str
    is_self: bool


@dataclass(frozen=True, slots=True)
class SubOutOptions:
    choices: list[SubOutChoice]
    game_on: bool


def sub_out_options(match_id: int, player: Player) -> SubOutOptions:
    """Who this player could ask to sub out. Checked again when the request is made."""
    match = Match.objects.filter(pk=match_id).first()
    if match is None:
        raise StaleAction("That match no longer exists.")
    _require_subbable(match)
    me = require_participant(match, player)
    choices = []
    if not _unresolved(match, player.pk):
        choices.append(SubOutChoice(player.pk, player.display_name, True))
    if me.is_captain and match.status != Status.READY_CHECK:
        others = (
            active_participants(match)
            .filter(is_captain=False)
            .filter(Q(team=me.team) | Q(team=""))
            .order_by("id")
        )
        for other in others:
            if not _unresolved(match, other.player_id):
                choices.append(SubOutChoice(other.player_id, other.player.display_name, False))
    if not choices:
        raise InvalidAction("You've already asked for a sub.")
    return SubOutOptions(choices, _game_on(match))


@transaction.atomic
def request_sub_out(match_id: int, requester: Player, leaving_player_id: int) -> SubRequest:
    match = lock_match(match_id)
    _require_subbable(match)
    me = require_participant(match, requester)
    leaving = active_participants(match).filter(player_id=leaving_player_id).first()
    if leaving is None:
        raise InvalidAction("That player isn't in this match.")
    if _unresolved(match, leaving_player_id):
        raise InvalidAction(f"There's already a sub request for {leaving.player.display_name}.")
    if match.sub_requests.filter(status__in=UNRESOLVED).count() >= MAX_PENDING:
        raise InvalidAction("Too many subs are pending. Wait for one to finish, or ask staff.")
    mode = mode_of(match)
    moment = now()
    if leaving_player_id == requester.pk:
        request = SubRequest.objects.create(
            match=match,
            leaving=requester,
            requested_by=requester,
            status=SubStatus.OPEN,
            deadline_at=moment,
            created_at=moment,
        )
        _open(match, mode, request)
        return request

    if not me.is_captain or match.status == Status.READY_CHECK:
        raise NotAllowed("Only a captain can ask to sub someone else out.")
    if leaving.is_captain:
        raise NotAllowed("Only that captain or staff can sub a captain out.")
    if leaving.team not in {"", me.team}:
        raise NotAllowed("You can only sub out your own teammates.")
    request = SubRequest.objects.create(
        match=match,
        leaving=leaving.player,
        requested_by=requester,
        status=SubStatus.AWAITING_APPROVAL,
        deadline_at=moment + timedelta(seconds=mode.subs.approve_seconds),
        created_at=moment,
    )
    _hold(match)  # Nobody gets auto-picked, or drafted away, while the captains decide.
    other = [c for c in _captains(match) if c.player_id != requester.pk]
    _ping(
        match,
        [c.player.discord_user_id for c in other],
        f"**{requester.display_name}** wants to sub out **{leaving.player.display_name}**. "
        "Approve or refuse on the board.",
    )
    render_board(match)
    return request


@transaction.atomic
def answer_sub_out(request_id: int, player: Player, approve: bool) -> None:
    """The other captain agrees, or not, to a captain subbing out a teammate."""
    match, request = _lock_request(request_id)
    if request.status != SubStatus.AWAITING_APPROVAL:
        raise StaleAction("That request has already been answered.")
    _require_subbable(match)
    me = require_participant(match, player)
    if not me.is_captain or player.pk == request.requested_by_id:
        raise NotAllowed("Only the other captain can answer this.")
    if approve:
        _open(match, mode_of(match), request)
        return
    _resolve(request, SubStatus.REJECTED)
    _ping(
        match,
        [request.requested_by.discord_user_id],
        f"**{player.display_name}** kept **{request.leaving.display_name}** in the match.",
    )
    bump(match)
    _release_if_clear(match)


def _open(match: Match, mode: DraftMode, request: SubRequest) -> None:
    moment = now()
    request.status = SubStatus.OPEN
    request.opened_at = moment
    request.deadline_at = moment + timedelta(seconds=mode.subs.max_wait_seconds)
    request.name_until = moment + timedelta(seconds=mode.subs.name_seconds)
    request.save(update_fields=["status", "opened_at", "deadline_at", "name_until"])
    _hold(match)
    stamp = int(request.name_until.timestamp())
    user_ids = {request.requested_by.discord_user_id, request.leaving.discord_user_id}
    user_ids.update(c.player.discord_user_id for c in _captains(match))
    _ping(
        match,
        sorted(user_ids),
        f"**{request.leaving.display_name}** is subbing out, so the match is paused. "
        f"Name a sub on the board, or the queue gets asked <t:{stamp}:R>.",
    )
    render_board(match)


@transaction.atomic
def cancel_sub(request_id: int, player: Player) -> None:
    """Whoever asked can take it back until someone has accepted."""
    match, request = _lock_request(request_id)
    if request.status not in UNRESOLVED:
        raise StaleAction("That sub request has already finished.")
    if player.pk != request.requested_by_id:
        raise NotAllowed("Only whoever asked for the sub can cancel it.")
    _withdraw_offers(request)
    _resolve(request, SubStatus.CANCELLED)
    bump(match)
    _release_if_clear(match)


# --- Sub in ----------------------------------------------------------------------------------


def _can_invite(match: Match, request: SubRequest, player: Player) -> bool:
    if player.pk == request.requested_by_id:
        return True
    if request.is_self and player.pk == request.leaving_id:
        return True
    leaving = match.participants.filter(player_id=request.leaving_id, is_active=True).first()
    if leaving is None or not leaving.team:
        return False
    return match.participants.filter(
        player=player, is_active=True, is_captain=True, team=leaving.team
    ).exists()


def check_can_invite(request_id: int, player: Player) -> None:
    """Fast check before showing the picker. The invite checks again under the lock."""
    request = SubRequest.objects.select_related("match").filter(pk=request_id).first()
    if request is None or request.status != SubStatus.OPEN:
        raise StaleAction("That spot has already been filled or closed.")
    if not _can_invite(request.match, request, player):
        raise NotAllowed("Only the player leaving or their captain can name a sub.")
    if request.offers.filter(status=OfferStatus.PENDING).exists():
        raise InvalidAction("Someone has already been asked. Wait for their answer.")


def _check_eligible(match: Match, request: SubRequest, player: Player) -> None:
    live = MatchParticipant.objects.filter(player=player, is_active=True).first()
    if live is not None:
        raise InvalidAction(f"{player.display_name} is already in match #{live.match_id}.")
    if match.participants.filter(player=player).exists():
        raise InvalidAction(f"{player.display_name} was already part of this match.")
    if penalties.active_penalty(match.community, player) is not None:
        raise InvalidAction(f"{player.display_name} is on a queue cooldown.")
    if request.offers.filter(player=player).exists():
        raise InvalidAction(f"{player.display_name} has already been asked for this spot.")


@transaction.atomic
def invite_sub(
    request_id: int, inviter: Player, discord_user_id: int, display_name: str
) -> SubOffer:
    """Ask a named person to take the slot. They have to accept."""
    match, request = _lock_request(request_id)
    if request.status != SubStatus.OPEN:
        raise StaleAction("That spot has already been filled or closed.")
    if not _can_invite(match, request, inviter):
        raise NotAllowed("Only the player leaving or their captain can name a sub.")
    if request.offers.filter(status=OfferStatus.PENDING).exists():
        raise InvalidAction("Someone has already been asked. Wait for their answer.")
    target = get_or_create_player(discord_user_id, display_name)
    _check_eligible(match, request, target)
    offer = _make_offer(match, request, target, OfferSource.INVITE, inviter)
    request.name_until = None  # Named. If they say no, the queue is next.
    request.save(update_fields=["name_until"])
    bump(match)
    render_board(match)
    return offer


@transaction.atomic
def offer_to_queue(request_id: int, player: Player) -> None:
    """Skip naming someone and go straight to the queue."""
    match, request = _lock_request(request_id)
    if request.status != SubStatus.OPEN:
        raise StaleAction("That spot has already been filled or closed.")
    if not _can_invite(match, request, player):
        raise NotAllowed("Only the player leaving or their captain can choose that.")
    if request.offers.filter(status=OfferStatus.PENDING).exists():
        raise InvalidAction("Someone has already been asked. Wait for their answer.")
    request.name_until = None
    request.save(update_fields=["name_until"])
    if not _offer_next(match, request):
        _ping(
            match,
            [player.discord_user_id],
            "nobody in the queue can take the spot yet. "
            "It goes to the first person who joins, or you can still name someone.",
        )
    bump(match)
    render_board(match)


def _make_offer(
    match: Match,
    request: SubRequest,
    player: Player,
    source: str,
    invited_by: Player | None = None,
) -> SubOffer:
    subs = mode_of(match).subs
    seconds = subs.invite_seconds if source == OfferSource.INVITE else subs.offer_seconds
    moment = now()
    offer = SubOffer.objects.create(
        request=request,
        player=player,
        source=source,
        invited_by=invited_by,
        deadline_at=moment + timedelta(seconds=seconds),
        created_at=moment,
    )
    add_effect(match.community, "sub_offer", {"offer_id": offer.pk}, match=match)
    return offer


def _offer_next(match: Match, request: SubRequest) -> bool:
    """Offer the slot to the first person in the queue who hasn't been asked. False if nobody."""
    asked = set(request.offers.values_list("player_id", flat=True))
    entries = QueueEntry.objects.filter(community=match.community).select_related("player")
    for entry in entries:
        if entry.player_id in asked:
            continue
        try:
            _check_eligible(match, request, entry.player)
        except InvalidAction:
            continue
        _make_offer(match, request, entry.player, OfferSource.QUEUE)
        return True
    return False


@transaction.atomic
def answer_offer(offer_id: int, player: Player, accept: bool) -> None:
    offer = SubOffer.objects.select_related("request").filter(pk=offer_id).first()
    if offer is None:
        raise StaleAction("That offer no longer exists.")
    if offer.player_id != player.pk:
        raise NotAllowed("This offer isn't for you.")
    match = lock_match(offer.request.match_id)
    offer.refresh_from_db()
    request = offer.request
    request.refresh_from_db()
    if offer.status != OfferStatus.PENDING or request.status != SubStatus.OPEN:
        raise StaleAction("Too late: that spot has been filled or closed.")
    if not accept:
        _close_offer(offer, OfferStatus.DECLINED)
        _after_offer(match, request, f"**{player.display_name}** can't sub in.")
        return
    lobby.check_sub_is_free(match, player)
    _close_offer(offer, OfferStatus.ACCEPTED)
    _fill(match, request, player)


def _fill(match: Match, request: SubRequest, in_player: Player) -> None:
    leaving = match.participants.select_related("player").get(
        player_id=request.leaving_id, is_active=True
    )
    game_on = _game_on(match)
    request.filled_by = in_player
    _resolve(request, SubStatus.FILLED)
    _withdraw_offers(request)
    # Accepting means they're here, so they count as ready.
    lobby.swap_player(match, leaving, in_player, ready=True)
    if game_on:
        _penalise(match, leaving.player)
    user_ids = {in_player.discord_user_id}
    user_ids.update(c.player.discord_user_id for c in _captains(match))
    _ping(
        match,
        sorted(user_ids),
        f"**{in_player.display_name}** is in for **{leaving.player.display_name}**.",
    )
    _release_if_clear(match)
    logger.info("Sub filled", extra={"match": match.pk, "request": request.pk})


def _penalise(match: Match, player: Player) -> None:
    penalties.issue(
        match.community,
        player,
        PenaltyKind.LEAVER,
        match=match,
        reason=f"Subbed out of match #{match.pk} after the game started",
    )


@transaction.atomic
def fail_offer(offer_id: int) -> None:
    """The bot couldn't reach them, so move on to the next person."""
    offer = SubOffer.objects.select_related("request", "player").get(pk=offer_id)
    match = lock_match(offer.request.match_id)
    offer.refresh_from_db()
    if offer.status != OfferStatus.PENDING:
        return
    _close_offer(offer, OfferStatus.FAILED)
    request = offer.request
    request.refresh_from_db()
    _after_offer(match, request, f"I couldn't DM **{offer.player.display_name}**.")


def _after_offer(match: Match, request: SubRequest, why: str) -> None:
    """An offer ended without a sub. Fall back to the queue, as a named invite would have."""
    if request.status == SubStatus.OPEN:
        if request.name_until is not None:
            request.name_until = None
            request.save(update_fields=["name_until"])
        if _offer_next(match, request):
            why += " Asking the next person in the queue."
        else:
            why += " Nobody in the queue can take it yet. You can still name someone."
        _ping(match, [request.requested_by.discord_user_id], why)
    bump(match)
    render_board(match)


def _close_offer(offer: SubOffer, status: str) -> None:
    offer.status = status
    offer.resolved_at = now()
    offer.save(update_fields=["status", "resolved_at"])


def _withdraw_offers(request: SubRequest) -> None:
    request.offers.filter(status=OfferStatus.PENDING).update(
        status=OfferStatus.WITHDRAWN, resolved_at=now()
    )


def _resolve(request: SubRequest, status: str) -> None:
    request.status = status
    request.resolved_at = now()
    request.save(update_fields=["status", "filled_by", "resolved_at"])


# --- The hold --------------------------------------------------------------------------------


def _hold(match: Match) -> None:
    """Park the match's timer. The open request's own deadline stands in for it."""
    if match.held_seconds is not None:
        bump(match)
        return
    left = 0
    if match.deadline_at is not None:
        left = max(0, int((match.deadline_at - now()).total_seconds()))
    match.held_seconds = left
    match.deadline_at = None
    bump(match, "held_seconds", "deadline_at")


def _release_if_clear(match: Match) -> None:
    """Once no request is left, restart the timer where it stopped and catch up on what waited."""
    if match.held_seconds is None or match.sub_requests.filter(status__in=UNRESOLVED).exists():
        render_board(match)
        return
    moment = now()
    match.deadline_at = moment + timedelta(seconds=max(match.held_seconds, RESUME_MIN_SECONDS))
    match.held_seconds = None
    bump(match, "held_seconds", "deadline_at")
    soonest = moment + timedelta(seconds=RESUME_MIN_SECONDS)
    CaptainHandoff.objects.filter(
        match=match, status=HandoffStatus.PENDING, deadline_at__lt=soonest
    ).update(deadline_at=soonest)
    if match.status == Status.READY_CHECK:
        if not active_participants(match).filter(ready_at__isnull=True).exists():
            lobby.begin_captain_phase(match)
            return
    elif match.status == Status.CAPTAIN_PHASE:
        lobby.advance_captain_phase(match)
        return
    render_board(match)


def after_staff_swap(match: Match, out_player: Player) -> None:
    """Staff swapped someone who had a request of their own: it's done, so close it."""
    rows = match.sub_requests.filter(leaving=out_player, status__in=UNRESOLVED)
    for request in rows:
        _withdraw_offers(request)
        _resolve(request, SubStatus.CANCELLED)
    _release_if_clear(match)


# --- Deadlines -------------------------------------------------------------------------------


def _expire_request(request_id: int, moment: datetime) -> bool:
    with transaction.atomic():
        match, request = _lock_request(request_id)
        if request.status not in UNRESOLVED or request.deadline_at > moment:
            return False
        if request.status == SubStatus.AWAITING_APPROVAL:
            _resolve(request, SubStatus.REJECTED)
            _ping(
                match,
                [request.requested_by.discord_user_id],
                f"the other captain didn't answer, so **{request.leaving.display_name}** stays in.",
            )
            bump(match)
            _release_if_clear(match)
            return True
        _withdraw_offers(request)
        _resolve(request, SubStatus.EXPIRED)
        if _game_on(match):
            _penalise(match, request.leaving)
        leaving = set(
            match.sub_requests.filter(status__in=UNRESOLVED).values_list("leaving_id", flat=True)
        )
        leaving.add(request.leaving_id)
        lobby.void_live(
            match,
            source=Source.TIMEOUT,
            reason=f"No sub was found for {request.leaving.display_name}",
            requeue=True,
            skip_requeue=leaving,
        )
        return True


def _expire_offer(offer_id: int, moment: datetime) -> bool:
    with transaction.atomic():
        offer = SubOffer.objects.select_related("request", "player").get(pk=offer_id)
        match = lock_match(offer.request.match_id)
        offer.refresh_from_db()
        if offer.status != OfferStatus.PENDING or offer.deadline_at > moment:
            return False
        _close_offer(offer, OfferStatus.EXPIRED)
        request = offer.request
        request.refresh_from_db()
        _after_offer(match, request, f"**{offer.player.display_name}** didn't answer in time.")
        return True


def _try_queue(request_id: int, moment: datetime) -> bool:
    """Nobody was named in time, or the queue was empty before: try the queue again."""
    with transaction.atomic():
        match, request = _lock_request(request_id)
        if request.status != SubStatus.OPEN:
            return False
        if request.name_until is not None and request.name_until > moment:
            return False
        if request.offers.filter(status=OfferStatus.PENDING).exists():
            return False
        changed = request.name_until is not None
        if changed:
            request.name_until = None
            request.save(update_fields=["name_until"])
        changed = _offer_next(match, request) or changed
        if changed:
            bump(match)
            render_board(match)
        return changed


def tick(moment: datetime) -> int:
    """One poller pass over sub deadlines. A failure in one request cannot block the others."""
    fired = 0
    jobs = [
        (
            _expire_request,
            SubRequest.objects.filter(status__in=UNRESOLVED, deadline_at__lte=moment),
        ),
        (
            _expire_offer,
            SubOffer.objects.filter(status=OfferStatus.PENDING, deadline_at__lte=moment),
        ),
        (
            _try_queue,
            SubRequest.objects.filter(status=SubStatus.OPEN)
            .filter(Q(name_until__isnull=True) | Q(name_until__lte=moment))
            .exclude(offers__status=OfferStatus.PENDING),
        ),
    ]
    for job, rows in jobs:
        for row_id in list(rows.values_list("pk", flat=True).distinct()):
            try:
                fired += 1 if job(row_id, moment) else 0
            except Exception:
                logger.exception("Sub deadline failed", extra={"row": row_id, "job": job.__name__})
    return fired


def extend_past_due(new_deadline: datetime) -> int:
    count = SubRequest.objects.filter(status__in=UNRESOLVED, deadline_at__lt=new_deadline).update(
        deadline_at=new_deadline
    )
    count += SubOffer.objects.filter(
        status=OfferStatus.PENDING, deadline_at__lt=new_deadline
    ).update(deadline_at=new_deadline)
    return count


# --- Read models -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OfferInfo:
    """What a DM offering the slot needs to say."""

    offer_id: int
    match_id: int
    pending: bool
    discord_user_id: int
    player_id: int
    leaving_name: str
    team: str
    status: str
    source: str
    invited_by: str
    deadline_at: datetime


def offer_info(offer_id: int) -> OfferInfo:
    offer = SubOffer.objects.select_related(
        "request__match", "request__leaving", "player", "invited_by"
    ).get(pk=offer_id)
    request = offer.request
    match = request.match
    leaving = match.participants.filter(player_id=request.leaving_id).first()
    return OfferInfo(
        offer_id=offer.pk,
        match_id=match.pk,
        pending=offer.status == OfferStatus.PENDING and request.status == SubStatus.OPEN,
        discord_user_id=offer.player.discord_user_id,
        player_id=offer.player_id,
        leaving_name=request.leaving.display_name,
        team=leaving.team if leaving else "",
        status=match.status,
        source=offer.source,
        invited_by=offer.invited_by.display_name if offer.invited_by else "",
        deadline_at=offer.deadline_at,
    )
