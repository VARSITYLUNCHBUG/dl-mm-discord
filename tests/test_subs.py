"""Player-driven subs: sub out opens a slot and pauses the match, sub in fills it."""

from __future__ import annotations

import pytest

from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.models import (
    CaptainVote,
    Community,
    Match,
    OutboxEffect,
    Penalty,
    Player,
    QueueEntry,
    RatingEvent,
    SubOffer,
    SubRequest,
)
from league.models.moderation import PenaltyKind
from league.models.subs import OfferSource, OfferStatus, SubStatus
from league.services import boards, deadlines, draft, lobby, queue, results, subs
from league.services.common import captain_of, mode_of
from tests.conftest import Clock, make_player
from tests.helpers import (
    finish,
    fresh,
    image,
    ready_all,
    run_player_draft,
    to_champion_draft,
    to_in_progress,
    vote_captains,
)

pytestmark = pytest.mark.django_db


def self_sub(match: Match, player: Player) -> SubRequest:
    return subs.request_sub_out(match.pk, player, player.pk)


def invite(request: SubRequest, inviter: Player, target: Player) -> SubOffer:
    return subs.invite_sub(request.pk, inviter, target.discord_user_id, target.display_name)


def queue_up(community: Community, numbers: range) -> list[Player]:
    """Players waiting in the queue, too few to pop a lobby of their own."""
    waiting = [make_player(number) for number in numbers]
    for player in waiting:
        queue.join(community.pk, player)
    return waiting


def in_draft(popped: Match) -> Match:
    return vote_captains(ready_all(popped))


def non_captain(match: Match, team: str = "") -> Player:
    rows = match.participants.filter(is_active=True, is_captain=False)
    if team:
        rows = rows.filter(team=team)
    return rows.order_by("id").first().player


# --- Sub out ---------------------------------------------------------------------------------


def test_subbing_yourself_out_pauses_the_match(popped: Match, players: list[Player]) -> None:
    request = self_sub(popped, players[3])
    match = fresh(popped)
    assert request.status == SubStatus.OPEN
    assert match.deadline_at is None
    assert match.held_seconds is not None and 170 <= match.held_seconds <= 180
    with pytest.raises(InvalidAction, match="paused"):
        lobby.mark_ready(match.pk, players[0])


def test_paused_board_hides_the_phase_buttons_and_shows_the_sub(
    popped: Match, players: list[Player]
) -> None:
    self_sub(popped, players[3])
    state = boards.board_state(popped.pk)
    assert state.on_hold
    assert [line.leaving_name for line in state.subs] == [players[3].display_name]
    assert state.hold_until is not None


def test_only_one_open_request_per_player(popped: Match, players: list[Player]) -> None:
    self_sub(popped, players[3])
    with pytest.raises(InvalidAction):
        self_sub(popped, players[3])


def test_stranger_cannot_ask_for_a_sub(popped: Match) -> None:
    outsider = make_player(90)
    with pytest.raises(InvalidAction):
        subs.request_sub_out(popped.pk, outsider, outsider.pk)


def test_nobody_but_a_captain_can_sub_out_someone_else(
    popped: Match, players: list[Player]
) -> None:
    with pytest.raises(NotAllowed):
        subs.request_sub_out(popped.pk, players[0], players[1].pk)


def test_captain_request_needs_the_other_captain(popped: Match) -> None:
    match = in_draft(popped)
    captain_a = captain_of(match, "A").player
    captain_b = captain_of(match, "B").player
    target = non_captain(match)
    request = subs.request_sub_out(match.pk, captain_a, target.pk)
    assert request.status == SubStatus.AWAITING_APPROVAL
    assert fresh(match).held_seconds is not None  # Nobody gets drafted away while they decide.
    with pytest.raises(NotAllowed):
        subs.answer_sub_out(request.pk, captain_a, True)
    subs.answer_sub_out(request.pk, captain_b, True)
    request.refresh_from_db()
    assert request.status == SubStatus.OPEN
    assert fresh(match).held_seconds is not None


def test_other_captain_can_refuse(popped: Match) -> None:
    match = in_draft(popped)
    target = non_captain(match)
    request = subs.request_sub_out(match.pk, captain_of(match, "A").player, target.pk)
    subs.answer_sub_out(request.pk, captain_of(match, "B").player, False)
    request.refresh_from_db()
    assert request.status == SubStatus.REJECTED
    match = fresh(match)
    assert match.participants.filter(player=target, is_active=True).exists()
    assert match.held_seconds is None
    assert match.deadline_at is not None


def test_unanswered_captain_request_lapses(popped: Match, clock: Clock) -> None:
    match = in_draft(popped)
    request = subs.request_sub_out(match.pk, captain_of(match, "A").player, non_captain(match).pk)
    clock.advance(mode_of(match).subs.approve_seconds + 1)
    deadlines.tick()
    request.refresh_from_db()
    assert request.status == SubStatus.REJECTED
    assert fresh(match).held_seconds is None


def test_at_most_four_requests_pending_at_once(popped: Match, players: list[Player]) -> None:
    for player in players[:4]:
        self_sub(popped, player)
    with pytest.raises(InvalidAction, match="Too many"):
        self_sub(popped, players[4])


def test_captain_cannot_sub_out_the_other_teams_players(popped: Match) -> None:
    match = run_player_draft(in_draft(popped))
    with pytest.raises(NotAllowed):
        subs.request_sub_out(match.pk, captain_of(match, "A").player, non_captain(match, "B").pk)
    with pytest.raises(NotAllowed):
        subs.request_sub_out(
            match.pk, captain_of(match, "A").player, captain_of(match, "B").player.pk
        )


def test_no_captain_requests_during_the_ready_check(popped: Match, players: list[Player]) -> None:
    options = subs.sub_out_options(popped.pk, players[0])
    assert [choice.is_self for choice in options.choices] == [True]


def test_captain_sees_teammates_and_undrafted_players(popped: Match) -> None:
    match = in_draft(popped)
    options = subs.sub_out_options(match.pk, captain_of(match, "A").player)
    assert options.choices[0].is_self
    assert len(options.choices) == 11  # Themself and the ten still to be drafted.


def test_whoever_asked_can_cancel_and_the_timer_resumes(
    popped: Match, players: list[Player]
) -> None:
    request = self_sub(popped, players[3])
    with pytest.raises(NotAllowed):
        subs.cancel_sub(request.pk, players[4])
    subs.cancel_sub(request.pk, players[3])
    match = fresh(popped)
    assert match.held_seconds is None
    assert match.deadline_at is not None
    lobby.mark_ready(match.pk, players[0])  # No longer paused.


# --- Sub in ----------------------------------------------------------------------------------


def test_named_sub_accepts_and_takes_the_spot(popped: Match, players: list[Player]) -> None:
    sub = make_player(77)
    request = self_sub(popped, players[3])
    offer = invite(request, players[3], sub)
    assert offer.source == OfferSource.INVITE
    assert OutboxEffect.objects.filter(kind="sub_offer", payload__offer_id=offer.pk).exists()
    subs.answer_offer(offer.pk, sub, True)
    match = fresh(popped)
    assert match.participants.filter(player=sub, is_active=True, ready_at__isnull=False).exists()
    assert not match.participants.filter(player=players[3], is_active=True).exists()
    assert match.held_seconds is None
    assert match.deadline_at is not None
    request.refresh_from_db()
    assert request.status == SubStatus.FILLED
    assert request.filled_by == sub


def test_sub_completing_the_ready_check_starts_the_captain_phase(
    popped: Match, players: list[Player]
) -> None:
    for player in players[:11]:
        lobby.mark_ready(popped.pk, player)
    request = self_sub(popped, players[11])
    sub = make_player(77)
    subs.answer_offer(invite(request, players[11], sub).pk, sub, True)
    assert fresh(popped).status == Status.CAPTAIN_PHASE


def test_only_the_person_asked_can_answer(popped: Match, players: list[Player]) -> None:
    request = self_sub(popped, players[3])
    offer = invite(request, players[3], make_player(77))
    with pytest.raises(NotAllowed):
        subs.answer_offer(offer.pk, make_player(78), True)


def test_one_person_asked_at_a_time(popped: Match, players: list[Player]) -> None:
    request = self_sub(popped, players[3])
    invite(request, players[3], make_player(77))
    with pytest.raises(InvalidAction, match="already been asked"):
        invite(request, players[3], make_player(78))


def test_cannot_invite_someone_in_a_match_or_already_out(
    popped: Match, players: list[Player]
) -> None:
    request = self_sub(popped, players[3])
    with pytest.raises(InvalidAction, match="already in match"):
        invite(request, players[3], players[4])


def test_cannot_invite_someone_on_a_cooldown(
    popped: Match, players: list[Player], league: Community
) -> None:
    from league.services import penalties

    banned = make_player(77)
    penalties.issue(league, banned, PenaltyKind.MANUAL, minutes=30)
    request = self_sub(popped, players[3])
    with pytest.raises(InvalidAction, match="cooldown"):
        invite(request, players[3], banned)


def test_only_the_leaver_or_their_captain_can_name_a_sub(popped: Match) -> None:
    match = run_player_draft(in_draft(popped))
    leaving = non_captain(match, "A")
    request = self_sub(match, leaving)
    with pytest.raises(NotAllowed):
        invite(request, captain_of(match, "B").player, make_player(77))
    offer = invite(request, captain_of(match, "A").player, make_player(77))
    assert offer.invited_by == captain_of(match, "A").player


def test_decline_falls_back_to_the_queue(
    popped: Match, players: list[Player], league: Community
) -> None:
    waiting = queue_up(league, range(30, 33))
    request = self_sub(popped, players[3])
    named = make_player(77)
    subs.answer_offer(invite(request, players[3], named).pk, named, False)
    pending = SubOffer.objects.get(request=request, status=OfferStatus.PENDING)
    assert pending.player == waiting[0]
    assert pending.source == OfferSource.QUEUE


def test_queue_is_asked_when_nobody_is_named_in_time(
    popped: Match, players: list[Player], league: Community, clock: Clock
) -> None:
    waiting = queue_up(league, range(30, 33))
    request = self_sub(popped, players[3])
    clock.advance(mode_of(popped).subs.name_seconds + 1)
    deadlines.tick()
    pending = SubOffer.objects.get(request=request, status=OfferStatus.PENDING)
    assert pending.player == waiting[0]


def test_queue_offers_go_down_the_line_until_someone_accepts(
    popped: Match, players: list[Player], league: Community, clock: Clock
) -> None:
    waiting = queue_up(league, range(30, 33))
    request = self_sub(popped, players[3])
    subs.offer_to_queue(request.pk, players[3])
    clock.advance(mode_of(popped).subs.offer_seconds + 1)
    deadlines.tick()  # The first in line didn't answer.
    second = SubOffer.objects.get(request=request, status=OfferStatus.PENDING)
    assert second.player == waiting[1]
    subs.answer_offer(second.pk, waiting[1], True)
    assert fresh(popped).participants.filter(player=waiting[1], is_active=True).exists()
    # They left the queue for the match. Everyone else keeps their place.
    assert list(QueueEntry.objects.values_list("player_id", flat=True)) == [
        waiting[0].pk,
        waiting[2].pk,
    ]


def test_an_empty_queue_is_tried_again_as_people_join(
    popped: Match, players: list[Player], league: Community, clock: Clock
) -> None:
    request = self_sub(popped, players[3])
    subs.offer_to_queue(request.pk, players[3])
    assert not SubOffer.objects.filter(request=request).exists()
    late = queue_up(league, range(30, 31))[0]
    clock.advance(5)
    deadlines.tick()
    assert SubOffer.objects.get(request=request, status=OfferStatus.PENDING).player == late


def test_unfilled_slot_cancels_the_match_and_requeues_everyone_else(
    popped: Match, players: list[Player], clock: Clock
) -> None:
    for player in players[:3] + players[4:]:
        lobby.mark_ready(popped.pk, player)
    request = self_sub(popped, players[3])
    clock.advance(mode_of(popped).subs.max_wait_seconds + 1)
    deadlines.tick()
    request.refresh_from_db()
    assert request.status == SubStatus.EXPIRED
    assert fresh(popped).status == Status.VOIDED
    queued = set(QueueEntry.objects.values_list("player_id", flat=True))
    assert players[3].pk not in queued
    assert len(queued) == 11


def test_late_answer_after_the_slot_closed_is_stale(popped: Match, players: list[Player]) -> None:
    request = self_sub(popped, players[3])
    sub = make_player(77)
    offer = invite(request, players[3], sub)
    subs.cancel_sub(request.pk, players[3])
    with pytest.raises(StaleAction):
        subs.answer_offer(offer.pk, sub, True)


# --- Through the phases ----------------------------------------------------------------------


def test_timer_resumes_where_it_stopped(popped: Match, players: list[Player], clock: Clock) -> None:
    match = to_champion_draft(popped)  # Champion draft: 40 seconds a step.
    clock.advance(10)
    leaving = non_captain(match, "A")
    request = self_sub(match, leaving)
    clock.advance(120)  # Paused, so none of this counts.
    deadlines.tick()
    assert fresh(match).step_index == match.step_index
    sub = make_player(77)
    subs.answer_offer(invite(request, leaving, sub).pk, sub, True)
    left = (fresh(match).deadline_at - clock.now()).total_seconds()
    assert 28 <= left <= 31


def test_paused_draft_refuses_picks(popped: Match) -> None:
    match = in_draft(popped)
    step = build_plan(mode_of(match))[match.step_index]
    team = team_for_role(step.role, match.player_first_pick)
    target = non_captain(match)
    self_sub(match, non_captain(match, ""))
    with pytest.raises(InvalidAction, match="paused"):
        draft.pick_player(match.pk, captain_of(match, team).player, match.step_index, target.pk)


def test_drafted_player_is_replaced_on_the_same_team(popped: Match) -> None:
    match = run_player_draft(in_draft(popped))
    leaving = non_captain(match, "B")
    request = self_sub(match, leaving)
    sub = make_player(77)
    subs.answer_offer(invite(request, leaving, sub).pk, sub, True)
    assert fresh(match).participants.get(player=sub, is_active=True).team == "B"


def test_undrafted_sub_goes_into_the_pool(popped: Match) -> None:
    match = in_draft(popped)
    leaving = non_captain(match)
    request = self_sub(match, leaving)
    sub = make_player(77)
    subs.answer_offer(invite(request, leaving, sub).pk, sub, True)
    match = fresh(match)
    assert match.participants.get(player=sub, is_active=True).team == ""
    run_player_draft(match)
    assert fresh(match).participants.get(player=sub, is_active=True).team in {"A", "B"}


def test_captain_subbing_out_hands_the_role_to_the_sub(popped: Match) -> None:
    match = in_draft(popped)  # Captains exist once the vote has closed.
    assert match.status == Status.PLAYER_DRAFT
    leaving = captain_of(match, "A").player
    request = self_sub(match, leaving)
    sub = make_player(77)
    subs.answer_offer(invite(request, leaving, sub).pk, sub, True)
    match = fresh(match)
    assert captain_of(match, "A").player == sub
    assert match.participants.get(player=sub, is_active=True).is_captain
    run_player_draft(match)
    assert fresh(match).status == Status.SIDE_SELECT


def test_a_sub_during_the_vote_drops_the_leavers_votes(
    voting: Match, players: list[Player]
) -> None:
    lobby.cast_captain_votes(voting.pk, players[3], [players[5].pk])
    lobby.cast_captain_votes(voting.pk, players[0], [players[3].pk])
    request = self_sub(voting, players[3])
    sub = make_player(60)
    subs.answer_offer(invite(request, players[3], sub).pk, sub, True)
    match = fresh(voting)
    assert match.status == Status.CAPTAIN_PHASE  # The sub hasn't voted, so the vote stays open.
    assert not CaptainVote.objects.filter(match=match, voter=players[3]).exists()
    assert not CaptainVote.objects.filter(match=match, candidate=players[3]).exists()
    lobby.cast_captain_votes(match.pk, sub, [sub.pk])


def test_a_sub_during_the_side_claim_resumes_it(voting: Match) -> None:
    match = run_player_draft(vote_captains(voting))
    leaving = non_captain(match)
    request = self_sub(match, leaving)
    sub = make_player(61)
    subs.answer_offer(invite(request, leaving, sub).pk, sub, True)
    match = fresh(match)
    assert match.status == Status.SIDE_SELECT
    assert match.deadline_at is not None
    draft.claim_side(match.pk, captain_of(match, "A").player, "hk")
    assert fresh(match).status == Status.CHAMPION_DRAFT


def test_lobby_host_subbing_out_makes_the_sub_host(popped: Match) -> None:
    from tests.helpers import run_champion_draft

    match = run_champion_draft(run_player_draft(in_draft(popped)))
    host = match.lobby_code_host
    request = self_sub(match, host)
    sub = make_player(77)
    subs.answer_offer(invite(request, host, sub).pk, sub, True)
    match = fresh(match)
    assert match.lobby_code_host == sub
    draft.submit_lobby_code(match.pk, sub, "ABC123")
    assert fresh(match).status == Status.IN_PROGRESS


def test_leaving_mid_game_is_a_leaver_and_the_sub_takes_the_result(popped: Match) -> None:
    match = to_in_progress(popped)
    leaving = non_captain(match, "A")
    request = self_sub(match, leaving)
    sub = make_player(77)
    subs.answer_offer(invite(request, leaving, sub).pk, sub, True)
    assert Penalty.objects.filter(player=leaving, kind=PenaltyKind.LEAVER).exists()
    finish(fresh(match), "A")
    assert RatingEvent.objects.filter(player=sub, delta__gt=0).exists()
    assert not RatingEvent.objects.filter(player=leaving).exists()


def test_leaving_before_the_game_is_free(popped: Match, players: list[Player]) -> None:
    request = self_sub(popped, players[3])
    sub = make_player(77)
    subs.answer_offer(invite(request, players[3], sub).pk, sub, True)
    assert not Penalty.objects.filter(player=players[3]).exists()


def test_cancelling_mid_game_is_free(popped: Match) -> None:
    match = to_in_progress(popped)
    leaving = non_captain(match, "A")
    request = self_sub(match, leaving)
    subs.cancel_sub(request.pk, leaving)
    assert not Penalty.objects.filter(player=leaving).exists()


def test_a_result_closes_an_open_sub(popped: Match) -> None:
    match = to_in_progress(popped)
    request = self_sub(match, non_captain(match, "A"))
    results.submit_report(match.pk, captain_of(match, "A").player, "A", image())
    request.refresh_from_db()
    match = fresh(match)
    assert request.status == SubStatus.CANCELLED
    assert match.status == Status.AWAITING_CONFIRMATION
    assert match.held_seconds is None


def test_subs_close_once_a_result_is_in(popped: Match) -> None:
    match = to_in_progress(popped)
    results.submit_report(match.pk, captain_of(match, "A").player, "A", image())
    with pytest.raises(StaleAction):
        self_sub(match, non_captain(match, "A"))


def test_staff_swap_closes_the_players_own_request(popped: Match, players: list[Player]) -> None:
    request = self_sub(popped, players[3])
    lobby.substitute_live(popped.pk, players[3], make_player(77), actor=None)
    request.refresh_from_db()
    assert request.status == SubStatus.CANCELLED
    assert fresh(popped).held_seconds is None


def test_downtime_extends_sub_deadlines(popped: Match, players: list[Player], clock: Clock) -> None:
    request = self_sub(popped, players[3])
    clock.advance(1000)
    deadlines.extend_past_due()
    deadlines.tick()
    request.refresh_from_db()
    assert request.status == SubStatus.OPEN
