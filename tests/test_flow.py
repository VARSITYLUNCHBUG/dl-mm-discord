"""The match lifecycle on real Postgres, using only service calls."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.models import (
    Community,
    DraftAction,
    Match,
    MatchParticipant,
    OutboxEffect,
    Penalty,
    Player,
    PlayerRating,
    QueueEntry,
    RatingEvent,
    ResultSubmission,
)
from league.models.outbox import EffectStatus
from league.services import deadlines, dev, draft, integrity, lobby, queue, ratings, results
from league.services.common import captain_of
from tests.conftest import Clock, make_player
from tests.helpers import (
    finish,
    fresh,
    image,
    keep_captains,
    ready_all,
    run_champion_draft,
    run_player_draft,
    to_in_progress,
)

pytestmark = pytest.mark.django_db


def effects(match: Match, kind: str) -> list[OutboxEffect]:
    return list(OutboxEffect.objects.filter(match=match, kind=kind))


# --- Queue ---------------------------------------------------------------------------------


def test_eleven_players_do_not_pop(league: Community, players: list[Player]) -> None:
    for player in players[:11]:
        queue.join(league.pk, player)
    assert Match.objects.count() == 0
    assert QueueEntry.objects.count() == 11


def test_twelfth_player_pops_first_twelve_fifo(league: Community) -> None:
    everyone = [make_player(n) for n in range(1, 15)]
    for player in everyone:
        queue.join(league.pk, player)
    match = Match.objects.get()
    in_match = set(match.participants.values_list("player_id", flat=True))
    assert in_match == {player.pk for player in everyone[:12]}
    assert set(QueueEntry.objects.values_list("player_id", flat=True)) == {
        everyone[12].pk,
        everyone[13].pk,
    }
    assert match.status == Status.READY_CHECK


def test_double_join_is_rejected(league: Community, players: list[Player]) -> None:
    queue.join(league.pk, players[0])
    with pytest.raises(InvalidAction, match="already in the queue"):
        queue.join(league.pk, players[0])


def test_cannot_queue_while_in_a_live_match(popped: Match, players: list[Player]) -> None:
    with pytest.raises(InvalidAction, match="already in match"):
        queue.join(popped.community_id, players[0])


def test_database_blocks_two_live_matches_for_one_player(
    popped: Match, players: list[Player]
) -> None:
    other = Match.objects.create(
        community=popped.community,
        season=popped.season,
        mode_version=popped.mode_version,
        status=Status.READY_CHECK,
        rng_seed=1,
        created_at=popped.created_at,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        MatchParticipant.objects.create(
            match=other, player=players[0], queue_joined_at=popped.created_at
        )


def test_leave_removes_from_queue(league: Community, players: list[Player]) -> None:
    queue.join(league.pk, players[0])
    queue.leave(league.pk, players[0])
    assert QueueEntry.objects.count() == 0
    with pytest.raises(InvalidAction):
        queue.leave(league.pk, players[0])


def test_capacity_limit_holds_the_queue(league: Community) -> None:
    Community.objects.filter(pk=league.pk).update(max_concurrent_matches=1)
    for number in range(1, 25):
        queue.join(league.pk, make_player(number))
    assert Match.objects.count() == 1
    assert QueueEntry.objects.count() == 12


# --- Ready check ---------------------------------------------------------------------------


def test_pop_writes_lobby_and_gather_effects(popped: Match) -> None:
    assert len(effects(popped, "create_lobby")) == 1
    assert len(effects(popped, "gather_player")) == 12


def test_ready_deadline_is_180_seconds(popped: Match) -> None:
    remaining = (popped.deadline_at - popped.created_at).total_seconds()
    assert 179 <= remaining <= 181


def test_all_ready_moves_to_captain_phase(popped: Match) -> None:
    match = ready_all(popped)
    assert match.status == Status.CAPTAIN_PHASE
    assert match.participants.filter(is_captain=True).count() == 2


def test_stranger_cannot_ready(popped: Match) -> None:
    with pytest.raises(InvalidAction, match="not in this match"):
        lobby.mark_ready(popped.pk, make_player(99))


def test_ready_timeout_backfills_next_in_queue(
    popped: Match, league: Community, players: list[Player], clock: Clock
) -> None:
    waiting = [make_player(50), make_player(51)]
    for player in waiting:
        queue.join(league.pk, player)
    for player in players[:10]:
        lobby.mark_ready(popped.pk, player)

    clock.advance(181)
    deadlines.tick()

    match = fresh(popped)
    assert match.status == Status.READY_CHECK
    active = set(match.participants.filter(is_active=True).values_list("player_id", flat=True))
    assert waiting[0].pk in active
    assert waiting[1].pk in active
    assert players[10].pk not in active
    assert match.participants.filter(is_active=True, ready_at__isnull=False).count() == 10
    assert Penalty.objects.filter(player=players[10], kind="missed_ready").exists()
    # A fresh timer for the replacements.
    assert (match.deadline_at - clock.now()).total_seconds() > 170


def test_ready_timeout_without_replacements_voids_and_requeues(
    popped: Match, league: Community, players: list[Player], clock: Clock
) -> None:
    for player in players[:11]:
        lobby.mark_ready(popped.pk, player)
    clock.advance(181)
    deadlines.tick()

    match = fresh(popped)
    assert match.status == Status.VOIDED
    assert QueueEntry.objects.count() == 11
    assert not QueueEntry.objects.filter(player=players[11]).exists()
    assert not match.participants.filter(is_active=True).exists()
    assert match.rating_events.count() == 0
    # They hold their original order, and the next joiner goes behind them and pops a new lobby.
    order = list(QueueEntry.objects.values_list("player_id", flat=True))
    assert order == [player.pk for player in players[:11]]
    late = make_player(70)
    queue.join(league.pk, late)
    second = Match.objects.order_by("-id").first()
    assert second.pk != match.pk
    assert set(second.participants.values_list("player_id", flat=True)) == {
        *order,
        late.pk,
    }


def test_penalty_blocks_queueing_then_expires(
    popped: Match, league: Community, players: list[Player], clock: Clock
) -> None:
    clock.advance(181)
    deadlines.tick()
    with pytest.raises(InvalidAction, match="cooldown"):
        queue.join(league.pk, players[0])
    clock.advance(6 * 60)
    queue.join(league.pk, players[0])


def test_penalty_ladder_escalates(
    popped: Match, league: Community, players: list[Player], clock: Clock
) -> None:
    from league.services import penalties

    first = penalties.issue(league, players[0], "missed_ready")
    second = penalties.issue(league, players[0], "missed_ready")
    assert (first.expires_at - first.starts_at).total_seconds() == 5 * 60
    assert (second.expires_at - second.starts_at).total_seconds() == 15 * 60


# --- Captains ------------------------------------------------------------------------------


def seed(match: Match, player: Player, rating: int) -> None:
    ratings.seed_player(match.season, player, rating, actor=None)


def test_highest_elo_become_captains(popped: Match, players: list[Player]) -> None:
    seed(popped, players[4], 1500)
    seed(popped, players[7], 1400)
    match = ready_all(popped)
    assert captain_of(match, "A").player == players[4]
    assert captain_of(match, "B").player == players[7]


def non_captain(match: Match) -> Player:
    return match.participants.filter(is_captain=False, is_active=True).first().player


def test_only_a_captain_can_request_handoff(popped: Match) -> None:
    match = ready_all(popped)
    other = non_captain(match)
    with pytest.raises(NotAllowed, match="Only a captain"):
        lobby.request_handoff(match.pk, other, captain_of(match, "A").player_id)


def test_accepted_handoff_replaces_captain(popped: Match) -> None:
    match = ready_all(popped)
    old = captain_of(match, "A").player
    new = non_captain(match)
    handoff = lobby.request_handoff(match.pk, old, new.pk)
    assert captain_of(fresh(match), "A").player == old  # Nothing changes until they accept.
    lobby.respond_handoff(handoff.pk, new, accept=True)
    match = fresh(match)
    assert captain_of(match, "A").player == new
    old_row = match.participants.get(player=old)
    assert not old_row.is_captain
    assert old_row.team == ""  # Back in the draft pool.


def test_only_the_target_can_answer(popped: Match) -> None:
    match = ready_all(popped)
    target = non_captain(match)
    handoff = lobby.request_handoff(match.pk, captain_of(match, "A").player, target.pk)
    with pytest.raises(NotAllowed):
        lobby.respond_handoff(handoff.pk, captain_of(match, "B").player, accept=True)


def test_two_requests_limit_counts_declines(popped: Match) -> None:
    match = ready_all(popped)
    captain = captain_of(match, "A").player
    others = list(match.participants.filter(is_captain=False)[:3])
    for participant in others[:2]:
        handoff = lobby.request_handoff(match.pk, captain, participant.player_id)
        lobby.respond_handoff(handoff.pk, participant.player, accept=False)
    assert captain_of(fresh(match), "A").player == captain
    with pytest.raises(InvalidAction, match="all 2"):
        lobby.request_handoff(match.pk, captain, others[2].player_id)


def test_replacement_inherits_remaining_requests(popped: Match) -> None:
    match = ready_all(popped)
    captain = captain_of(match, "A").player
    others = list(match.participants.filter(is_captain=False)[:3])
    declined = lobby.request_handoff(match.pk, captain, others[0].player_id)
    lobby.respond_handoff(declined.pk, others[0].player, accept=False)
    accepted = lobby.request_handoff(match.pk, captain, others[1].player_id)
    lobby.respond_handoff(accepted.pk, others[1].player, accept=True)
    # The new captain accepted, so is confirmed, and team A's budget is spent.
    assert lobby.handoffs_used(fresh(match), "A") == 2


def test_cannot_hand_off_to_other_captain(popped: Match) -> None:
    match = ready_all(popped)
    with pytest.raises(InvalidAction, match="already a captain"):
        lobby.request_handoff(
            match.pk, captain_of(match, "A").player, captain_of(match, "B").player_id
        )


def test_expired_handoff_uses_a_request_and_keeps_captain(popped: Match, clock: Clock) -> None:
    match = ready_all(popped)
    captain = captain_of(match, "A").player
    lobby.request_handoff(match.pk, captain, non_captain(match).pk)
    clock.advance(31)
    deadlines.tick()
    match = fresh(match)
    assert lobby.handoffs_used(match, "A") == 1
    assert match.status == Status.PLAYER_DRAFT
    assert captain_of(match, "A").player == captain


def test_captain_phase_timeout_keeps_originals(popped: Match, clock: Clock) -> None:
    match = ready_all(popped)
    captain_a = captain_of(match, "A").player
    clock.advance(31)
    deadlines.tick()
    match = fresh(match)
    assert match.status == Status.PLAYER_DRAFT
    assert captain_of(match, "A").player == captain_a


def test_pending_handoff_outlives_phase_deadline(popped: Match, clock: Clock) -> None:
    match = ready_all(popped)
    lobby.keep_captaincy(match.pk, captain_of(match, "B").player)
    clock.advance(15)
    target = non_captain(match)
    handoff = lobby.request_handoff(match.pk, captain_of(match, "A").player, target.pk)
    clock.advance(6)  # The phase's own 20 seconds have passed; the request has 24 left.
    deadlines.tick()
    assert fresh(match).status == Status.CAPTAIN_PHASE
    lobby.respond_handoff(handoff.pk, target, accept=True)
    match = fresh(match)
    assert match.status == Status.PLAYER_DRAFT
    assert captain_of(match, "A").player == target


def test_handoff_closed_once_draft_starts(popped: Match) -> None:
    match = keep_captains(ready_all(popped))
    with pytest.raises(StaleAction):
        lobby.request_handoff(match.pk, captain_of(match, "A").player, non_captain(match).pk)


# --- Player draft --------------------------------------------------------------------------


def test_lower_rated_captain_gets_first_pick(popped: Match, players: list[Player]) -> None:
    seed(popped, players[0], 1500)
    seed(popped, players[1], 1400)
    match = keep_captains(ready_all(popped))
    assert captain_of(match, "B").player == players[1]
    assert match.player_first_pick == "B"


def test_first_pick_recomputed_after_handoff(popped: Match, players: list[Player]) -> None:
    seed(popped, players[0], 1500)
    seed(popped, players[1], 1400)
    seed(popped, players[2], 1100)
    match = ready_all(popped)
    handoff = lobby.request_handoff(match.pk, players[0], players[2].pk)
    lobby.respond_handoff(handoff.pk, players[2], accept=True)
    lobby.keep_captaincy(match.pk, players[1])
    match = fresh(match)
    assert captain_of(match, "A").player == players[2]
    assert match.player_first_pick == "A"  # Now team A's captain is the lower rated.


def test_draft_produces_two_full_teams(popped: Match) -> None:
    match = run_player_draft(keep_captains(ready_all(popped)))
    assert match.status == Status.CHAMPION_DRAFT
    assert match.participants.filter(team="A").count() == 6
    assert match.participants.filter(team="B").count() == 6
    actions = list(DraftAction.objects.filter(match=match, action="player_pick"))
    assert len(actions) == 10
    assert actions[-1].source == "system"  # The last player was forced.
    assert len(effects(match, "create_team_voice")) == 1


def test_pick_order_is_snake(popped: Match) -> None:
    match = run_player_draft(keep_captains(ready_all(popped)))
    first = match.player_first_pick
    second = "B" if first == "A" else "A"
    teams = "".join(
        "F" if action.team == first else "S"
        for action in DraftAction.objects.filter(match=match, action="player_pick")
    )
    assert second != first
    assert teams == "FSSFFSSFFS"


def test_wrong_captain_cannot_pick(popped: Match) -> None:
    match = keep_captains(ready_all(popped))
    second = "B" if match.player_first_pick == "A" else "A"
    target = match.participants.filter(team="").first()
    with pytest.raises(NotAllowed):
        draft.pick_player(match.pk, captain_of(match, second).player, 0, target.player_id)


def test_double_click_is_stale_and_changes_nothing(popped: Match) -> None:
    match = keep_captains(ready_all(popped))
    captain = captain_of(match, match.player_first_pick).player
    target = match.participants.filter(team="").first()
    draft.pick_player(match.pk, captain, 0, target.player_id, interaction_id=1)
    other = match.participants.filter(team="").first()
    with pytest.raises(StaleAction):
        draft.pick_player(match.pk, captain, 0, other.player_id, interaction_id=2)
    assert DraftAction.objects.filter(match=match).count() == 1
    assert fresh(match).step_index == 1


def test_cannot_pick_already_drafted_player(popped: Match) -> None:
    match = keep_captains(ready_all(popped))
    captain = captain_of(match, match.player_first_pick).player
    target = match.participants.filter(team="").first()
    draft.pick_player(match.pk, captain, 0, target.player_id)
    match = fresh(match)
    second = "B" if match.player_first_pick == "A" else "A"
    with pytest.raises(InvalidAction, match="already been drafted"):
        draft.pick_player(match.pk, captain_of(match, second).player, 1, target.player_id)


def test_player_pick_timeout_takes_highest_rated(
    popped: Match, players: list[Player], clock: Clock
) -> None:
    seed(popped, players[0], 1500)
    seed(popped, players[1], 1400)
    seed(popped, players[5], 1300)
    match = keep_captains(ready_all(popped))
    clock.advance(46)
    deadlines.tick()
    action = DraftAction.objects.get(match=match, step_index=0)
    assert action.source == "timeout"
    assert action.target_player == players[5]


# --- Champion draft ------------------------------------------------------------------------


def champion_ready(popped: Match) -> Match:
    return run_player_draft(keep_captains(ready_all(popped)))


def test_champion_draft_runs_18_steps_to_lobby_code(popped: Match) -> None:
    match = run_champion_draft(champion_ready(popped))
    assert match.status == Status.LOBBY_CODE
    actions = DraftAction.objects.filter(match=match).exclude(action="player_pick")
    assert actions.count() == 18
    assert actions.filter(action="ban").count() == 6
    assert actions.filter(action="pick", team="A").count() == 6
    assert actions.filter(action="pick", team="B").count() == 6
    assert match.lobby_code_host_id in match.participants.filter(is_captain=True).values_list(
        "player_id", flat=True
    )
    assert match.hidden_king_team in {"A", "B"}


def test_banned_champion_cannot_be_picked(popped: Match) -> None:
    from league.domain.draft_plan import build_plan, team_for_role
    from league.services.common import mode_of

    match = champion_ready(popped)
    plan = build_plan(mode_of(match))
    team = team_for_role(plan[match.step_index].role, match.champion_first_pick)
    champion = draft.available_champions(match)[0]
    draft.choose_champion(match.pk, captain_of(match, team).player, match.step_index, champion)
    match = fresh(match)
    team = team_for_role(plan[match.step_index].role, match.champion_first_pick)
    with pytest.raises(InvalidAction, match="isn't available"):
        draft.choose_champion(match.pk, captain_of(match, team).player, match.step_index, champion)


def test_disabled_champion_is_not_in_the_pool(league: Community, players: list[Player]) -> None:
    from league.models import Champion

    Champion.objects.filter(pk=1).update(is_enabled=False)
    for player in players:
        queue.join(league.pk, player)
    assert 1 not in Match.objects.get().champion_pool


def test_ban_timeout_skips_and_pick_timeout_randomises(popped: Match, clock: Clock) -> None:
    match = champion_ready(popped)
    for _ in range(18):
        clock.advance(31)
        deadlines.tick()
    match = fresh(match)
    assert match.status == Status.LOBBY_CODE
    bans = DraftAction.objects.filter(match=match, action="ban")
    assert bans.count() == 6
    assert all(ban.target_champion is None for ban in bans)
    picks = DraftAction.objects.filter(match=match, action="pick")
    assert picks.count() == 12
    assert picks.values("target_champion").distinct().count() == 12


# --- Lobby code ----------------------------------------------------------------------------


def test_only_host_can_submit_code(popped: Match) -> None:
    match = run_champion_draft(champion_ready(popped))
    other = match.participants.filter(is_captain=True).exclude(player=match.lobby_code_host).first()
    with pytest.raises(NotAllowed):
        draft.submit_lobby_code(match.pk, other.player, "ABC123")


def test_bad_code_rejected(popped: Match) -> None:
    match = run_champion_draft(champion_ready(popped))
    with pytest.raises(InvalidAction):
        draft.submit_lobby_code(match.pk, match.lobby_code_host, "<@everyone>")


def test_code_timeout_passes_to_other_captain_then_voids(popped: Match, clock: Clock) -> None:
    match = run_champion_draft(champion_ready(popped))
    first_host = match.lobby_code_host_id
    clock.advance(181)
    deadlines.tick()
    match = fresh(match)
    assert match.status == Status.LOBBY_CODE
    assert match.lobby_code_host_id != first_host
    clock.advance(181)
    deadlines.tick()
    assert fresh(match).status == Status.VOIDED


def test_code_starts_match_and_schedules_reminder(popped: Match) -> None:
    match = to_in_progress(popped)
    assert match.status == Status.IN_PROGRESS
    assert match.lobby_code == "ABC123"
    assert len(effects(match, "announce_lobby_code")) == 1
    reminder = effects(match, "report_reminder")[0]
    assert (reminder.run_after - match.started_at).total_seconds() > 40 * 60


# --- Results and ratings -------------------------------------------------------------------


def test_one_report_then_silence_finalises(popped: Match, clock: Clock) -> None:
    match = to_in_progress(popped)
    results.submit_report(match.pk, captain_of(match, "A").player, "A", image())
    assert fresh(match).status == Status.AWAITING_CONFIRMATION
    clock.advance(901)
    deadlines.tick()
    match = fresh(match)
    assert match.status == Status.COMPLETED
    assert match.winner == "A"
    assert match.results.get().source == "timeout"
    assert match.rating_events.count() == 12


def test_confirm_finalises_immediately_with_shared_delta(popped: Match) -> None:
    match = finish(to_in_progress(popped), "B")
    assert match.status == Status.COMPLETED
    assert match.winner == "B"
    winners = RatingEvent.objects.filter(
        match=match, player__in=[p.player for p in match.participants.filter(team="B")]
    )
    assert {event.delta for event in winners} == {16}
    assert sum(event.delta for event in RatingEvent.objects.filter(match=match)) == 0
    assert PlayerRating.objects.filter(season=match.season, rating=1016).count() == 6
    assert PlayerRating.objects.filter(season=match.season, rating=984).count() == 6
    assert integrity.check_all() == []


def test_participants_are_free_to_queue_after_finish(popped: Match, players: list[Player]) -> None:
    match = finish(to_in_progress(popped))
    assert not match.participants.filter(is_active=True).exists()
    queue.join(match.community_id, players[0])


def test_conflicting_report_goes_to_dispute_unrated(popped: Match) -> None:
    match = to_in_progress(popped)
    results.submit_report(match.pk, captain_of(match, "A").player, "A", image(1))
    results.submit_report(match.pk, captain_of(match, "B").player, "B", image(2))
    match = fresh(match)
    assert match.status == Status.DISPUTED
    assert RatingEvent.objects.count() == 0
    assert len(effects(match, "ping_staff")) == 1


def test_second_matching_screenshot_is_kept(popped: Match) -> None:
    match = to_in_progress(popped)
    results.submit_report(match.pk, captain_of(match, "A").player, "A", image(1))
    results.submit_report(match.pk, captain_of(match, "B").player, "A", image(2))
    match = fresh(match)
    assert match.status == Status.COMPLETED
    stored = ResultSubmission.objects.filter(match=match, archive_message_id__isnull=False)
    assert stored.count() == 2  # Both reach the analytics table; only the first is posted.


def test_dispute_button_then_staff_resolves(popped: Match) -> None:
    match = to_in_progress(popped)
    results.submit_report(match.pk, captain_of(match, "A").player, "A", image())
    results.dispute(match.pk, captain_of(match, "B").player, "We won that")
    assert fresh(match).status == Status.DISPUTED
    results.resolve_dispute(match.pk, "B", actor=None, reason="Checked the VOD")
    match = fresh(match)
    assert match.status == Status.COMPLETED
    assert match.winner == "B"
    assert match.results.get().source == "staff"


def test_staff_can_void_a_dispute_with_no_rating_change(popped: Match) -> None:
    match = to_in_progress(popped)
    results.submit_report(match.pk, captain_of(match, "A").player, "A", image())
    results.dispute(match.pk, captain_of(match, "B").player, "Crash")
    results.resolve_dispute(match.pk, "void", actor=None)
    assert fresh(match).status == Status.VOIDED
    assert RatingEvent.objects.count() == 0


def test_no_report_escalates_to_staff_after_three_hours(popped: Match, clock: Clock) -> None:
    match = to_in_progress(popped)
    clock.advance(3 * 3600 + 1)
    deadlines.tick()
    match = fresh(match)
    assert match.status == Status.DISPUTED
    assert RatingEvent.objects.count() == 0


def test_non_captain_cannot_report(popped: Match) -> None:
    match = to_in_progress(popped)
    other = match.participants.filter(is_captain=False).first().player
    with pytest.raises(NotAllowed):
        results.submit_report(match.pk, other, "A", image())


def test_same_captain_cannot_respond_twice(popped: Match) -> None:
    match = to_in_progress(popped)
    captain = captain_of(match, "A").player
    results.submit_report(match.pk, captain, "A", image())
    with pytest.raises(InvalidAction, match="already responded"):
        results.confirm(match.pk, captain)


def test_applying_same_revision_twice_is_a_noop(popped: Match) -> None:
    match = finish(to_in_progress(popped))
    before = RatingEvent.objects.count()
    ratings.apply_live_result(fresh(match), "A", revision=1)
    assert RatingEvent.objects.count() == before


def test_finish_writes_results_and_cleanup_effects(popped: Match) -> None:
    match = finish(to_in_progress(popped))
    assert len(effects(match, "post_results")) == 1
    assert len(effects(match, "cleanup_voice")) == 1
    delete = effects(match, "delete_text_channel")[0]
    assert delete.run_after > match.ended_at


def test_finishing_a_match_pops_a_held_queue(league: Community) -> None:
    Community.objects.filter(pk=league.pk).update(max_concurrent_matches=1)
    for number in range(1, 25):
        queue.join(league.pk, make_player(number))
    first = Match.objects.get()
    finish(to_in_progress(first))
    assert Match.objects.count() == 2


# --- Staff -----------------------------------------------------------------------------------


def test_staff_void_ends_live_match(popped: Match) -> None:
    lobby.staff_void(popped.pk, None, "Server issue")
    match = fresh(popped)
    assert match.status == Status.VOIDED
    assert not match.participants.filter(is_active=True).exists()


def test_live_substitute_inherits_captaincy_and_result(popped: Match) -> None:
    match = to_in_progress(popped)
    leaving = captain_of(match, "A").player
    sub = make_player(77)
    lobby.substitute_live(match.pk, leaving, sub, actor=None)
    match = fresh(match)
    assert captain_of(match, "A").player == sub
    finish(match, "A")
    assert RatingEvent.objects.filter(player=sub, delta=16).exists()
    assert not RatingEvent.objects.filter(player=leaving).exists()


def test_substitute_cannot_already_be_in_a_match(popped: Match, players: list[Player]) -> None:
    match = to_in_progress(popped)
    with pytest.raises(InvalidAction):
        lobby.substitute_live(match.pk, players[0], players[1], actor=None)


def test_failed_lobby_setup_requeues_everyone(popped: Match) -> None:
    lobby.fail_lobby_setup(popped.pk, "Discord refused")
    assert fresh(popped).status == Status.VOIDED
    assert QueueEntry.objects.count() == 12


# --- Deadlines -------------------------------------------------------------------------------


def test_restart_extends_past_due_deadlines(popped: Match, clock: Clock) -> None:
    clock.advance(1000)
    assert deadlines.extend_past_due() == 1
    assert deadlines.tick() == 0
    assert fresh(popped).status == Status.READY_CHECK
    clock.advance(61)
    deadlines.tick()
    assert fresh(popped).status != Status.READY_CHECK or fresh(popped).version > 0


def test_tick_ignores_deadlines_not_yet_due(popped: Match) -> None:
    assert deadlines.tick() == 0
    assert fresh(popped).status == Status.READY_CHECK


def test_render_requests_coalesce(popped: Match) -> None:
    lobby.mark_ready(popped.pk, popped.participants.first().player)
    lobby.mark_ready(popped.pk, popped.participants.last().player)
    pending = OutboxEffect.objects.filter(
        match=popped, kind="render_board", status=EffectStatus.PENDING
    )
    assert pending.count() == 1


# --- Smoke test tool -------------------------------------------------------------------------


def test_dev_autoplay_is_refused_outside_debug(league: Community, settings) -> None:
    settings.DEBUG = False
    settings.DEV_FAKE_PLAYERS = True
    with pytest.raises(CommandError, match="DEBUG"):
        call_command("dev_autoplay", pause=0, step=0)


def test_dev_autoplay_plays_a_whole_match(league: Community, settings) -> None:
    for player in dev.fake_players(12):
        queue.join(league.pk, player)
    first = Match.objects.get()
    finish(to_in_progress(first))  # The tool reuses an archived screenshot from an earlier match.
    settings.DEBUG = True
    settings.DEV_FAKE_PLAYERS = True
    call_command("dev_autoplay", pause=0, step=0, winner="B")
    match = Match.objects.order_by("-id").first()
    assert match.pk != first.pk
    assert match.status == Status.COMPLETED
    assert match.winner == "B"
    assert match.handoffs.filter(status="declined").count() == 1
    assert match.handoffs.filter(status="accepted").count() == 1
    assert integrity.check_all() == []
