"""Dodge strikes that escalate and can be played off, and the 5-match captain rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from league.domain.engine import CaptainCandidate, Status, tally_captain_votes
from league.domain.errors import InvalidAction
from league.domain.standing import (
    CaptainRule,
    StrikeEntry,
    StrikePolicy,
    captain_eligibility,
    fold_strikes,
    ladder_rung,
)
from league.models import Community, Match, OutboxEffect, Penalty, Player
from league.models.moderation import PenaltyKind
from league.services import corrections, lobby, penalties, standing, subs
from league.services.common import captain_of
from tests.conftest import Clock, make_player
from tests.helpers import fresh, play_full_match, ready_all, run_player_draft, vote_captains

T0 = datetime(2026, 1, 1, tzinfo=UTC)
POLICY = StrikePolicy(points={"dodge": 2, "missed_ready": 1}, games_per_strike=3, decay_days=14)


def at(days: float) -> datetime:
    return T0 + timedelta(days=days)


# --- The pure fold ---------------------------------------------------------------------------


def test_an_offence_adds_its_strikes() -> None:
    standing_ = fold_strikes([StrikeEntry(at(0), "offence", 2)], POLICY, at(1))
    assert standing_.strikes == 2
    assert standing_.games_to_next == 3
    assert standing_.next_decay_at == at(14)


def test_three_clean_games_remove_one_strike() -> None:
    entries = [StrikeEntry(at(0), "offence", 2)] + [
        StrikeEntry(at(0.1 * n), "clean", tiebreak=n) for n in range(1, 4)
    ]
    standing_ = fold_strikes(entries, POLICY, at(1))
    assert standing_.strikes == 1
    assert standing_.progress == 0


def test_an_offence_resets_progress_towards_the_next_strike() -> None:
    entries = [
        StrikeEntry(at(0), "offence", 1),
        StrikeEntry(at(0.1), "clean"),
        StrikeEntry(at(0.2), "clean"),
        StrikeEntry(at(0.3), "offence", 1),
        StrikeEntry(at(0.4), "clean"),
    ]
    standing_ = fold_strikes(entries, POLICY, at(1))
    assert standing_.strikes == 2
    assert standing_.progress == 1


def test_time_without_offences_removes_strikes() -> None:
    entries = [StrikeEntry(at(0), "offence", 2)]
    assert fold_strikes(entries, POLICY, at(13.9)).strikes == 2
    assert fold_strikes(entries, POLICY, at(14)).strikes == 1
    assert fold_strikes(entries, POLICY, at(28)).clean


def test_clean_games_do_nothing_on_a_clean_record() -> None:
    entries = [StrikeEntry(at(n), "clean") for n in range(5)] + [StrikeEntry(at(6), "offence", 1)]
    assert fold_strikes(entries, POLICY, at(7)).strikes == 1


def test_strikes_stay_between_zero_and_the_maximum() -> None:
    capped = StrikePolicy(points={"dodge": 4}, max_strikes=5)
    entries = [StrikeEntry(at(0), "offence", 4), StrikeEntry(at(0.1), "offence", 4)]
    assert fold_strikes(entries, capped, at(1)).strikes == 5
    entries.append(StrikeEntry(at(0.2), "adjust", -20))
    assert fold_strikes(entries, capped, at(1)).clean


def test_entries_after_the_moment_are_ignored() -> None:
    assert fold_strikes([StrikeEntry(at(5), "offence", 2)], POLICY, at(1)).clean


def test_the_ladder_climbs_with_strikes_already_carried() -> None:
    assert ladder_rung(0, 5) == 0
    assert ladder_rung(2, 5) == 2
    assert ladder_rung(9, 5) == 4


def test_captain_eligibility() -> None:
    rule = CaptainRule(min_games=5, max_strikes=3)
    assert captain_eligibility(5, 0, "", rule).ok
    assert captain_eligibility(4, 0, "", rule).reason == "4/5 matches"
    assert captain_eligibility(9, 3, "", rule).reason == "3 strikes"
    assert captain_eligibility(0, 9, "allow", rule).ok
    assert not captain_eligibility(50, 0, "bar", rule).ok
    assert captain_eligibility(0, 9, "", CaptainRule(min_games=0, max_strikes=None)).ok


def test_tally_puts_eligible_players_first_and_fills_by_experience() -> None:
    candidates = [
        CaptainCandidate(1, rating=2000, games=0, eligible=False, experience=1),
        CaptainCandidate(2, rating=900, games=0, eligible=True, experience=6),
        CaptainCandidate(3, rating=1500, games=0, eligible=False, experience=4),
    ]
    # Votes for an ineligible player never count, however many.
    assert tally_captain_votes({1: 9}, candidates, seed=1) == (2, 3)


# --- Dodges and cooldowns --------------------------------------------------------------------


def _non_captain(match: Match) -> Player:
    row = match.participants.filter(is_active=True, is_captain=False).order_by("id").first()
    assert row is not None
    return row.player


def _sub_in(match: Match, leaving: Player, number: int) -> Player:
    request = subs.request_sub_out(match.pk, leaving, leaving.pk)
    sub = make_player(number)
    offer = subs.invite_sub(request.pk, leaving, sub.discord_user_id, sub.display_name)
    subs.answer_offer(offer.pk, sub, True)
    return sub


def test_subbing_yourself_out_during_the_draft_is_a_dodge(voting: Match) -> None:
    match = vote_captains(voting)
    assert match.status == Status.PLAYER_DRAFT
    leaving = _non_captain(match)
    _sub_in(match, leaving, 50)
    penalty = Penalty.objects.get(player=leaving)
    assert penalty.kind == PenaltyKind.DODGE
    assert penalty.strikes == 2
    assert penalty.expires_at - penalty.starts_at == timedelta(minutes=60)


def test_subbing_out_during_the_ready_check_is_not_a_dodge(popped: Match) -> None:
    leaving = popped.participants.order_by("id").first().player
    _sub_in(popped, leaving, 51)
    assert not Penalty.objects.filter(player=leaving).exists()


def test_cancelling_a_sub_request_is_not_a_dodge(voting: Match) -> None:
    match = vote_captains(voting)
    leaving = _non_captain(match)
    request = subs.request_sub_out(match.pk, leaving, leaving.pk)
    subs.cancel_sub(request.pk, leaving)
    assert not Penalty.objects.filter(player=leaving).exists()


def test_the_sub_out_options_warn_about_a_dodge(voting: Match) -> None:
    match = vote_captains(voting)
    options = subs.sub_out_options(match.pk, _non_captain(match))
    assert options.dodge and not options.game_on


def test_repeat_offences_climb_the_ladder(league: Community, players: list[Player]) -> None:
    player = players[0]
    first = penalties.issue(league, player, PenaltyKind.DODGE)
    second = penalties.issue(league, player, PenaltyKind.DODGE)
    assert first.expires_at - first.starts_at == timedelta(minutes=60)
    # Two strikes already carried: rung three.
    assert second.expires_at - second.starts_at == timedelta(minutes=1440)
    assert standing.standing(league, player).strikes == 4


def test_strikes_from_any_kind_raise_the_next_cooldown(
    league: Community, players: list[Player]
) -> None:
    player = players[0]
    penalties.issue(league, player, PenaltyKind.DODGE)
    missed = penalties.issue(league, player, PenaltyKind.MISSED_READY)
    assert missed.expires_at - missed.starts_at == timedelta(
        minutes=60
    )  # Rung three of [5, 15, 60]


def test_revoking_a_cooldown_waives_its_strikes(league: Community, players: list[Player]) -> None:
    player = players[0]
    penalties.issue(league, player, PenaltyKind.DODGE)
    penalties.revoke_active(league, player)
    assert standing.standing(league, player).clean
    assert penalties.active_penalty(league, player) is None


def test_revoking_can_keep_the_strikes(league: Community, players: list[Player]) -> None:
    player = players[0]
    penalties.issue(league, player, PenaltyKind.DODGE)
    penalties.revoke_active(league, player, keep_strikes=True)
    assert standing.standing(league, player).strikes == 2


def test_staff_can_adjust_strikes(league: Community, players: list[Player]) -> None:
    after = standing.adjust_strikes(league, players[0], 3, None, "Reported for griefing")
    assert after.strikes == 3
    with pytest.raises(InvalidAction):
        standing.adjust_strikes(league, players[0], 1, None, " ")


def test_the_queue_refusal_says_how_to_clear_strikes(
    league: Community, players: list[Player]
) -> None:
    from league.services import queue

    penalties.issue(league, players[0], PenaltyKind.DODGE)
    with pytest.raises(InvalidAction, match="2 strikes: 3 more clean matches remove one"):
        queue.join(league.pk, players[0])


def test_clean_matches_play_strikes_off(
    league: Community, players: list[Player], clock: Clock
) -> None:
    player = players[0]
    penalties.issue(league, player, PenaltyKind.DODGE)
    clock.advance(3700)  # Past the hour's cooldown.
    for number in range(1, 4):
        play_full_match(league, players, "A", number)
    assert standing.standing(league, player).strikes == 1
    pings = OutboxEffect.objects.filter(kind="ping", payload__text__icontains="one strike off")
    assert pings.filter(payload__user_ids=[player.discord_user_id]).exists()


# --- The captain rule ------------------------------------------------------------------------


@pytest.fixture
def ruled_popped(captain_rule: Community, players: list[Player]) -> Match:
    from league.services import queue

    for player in players:
        queue.join(captain_rule.pk, player)
    return Match.objects.get()


def test_new_players_skip_the_vote_and_captains_come_by_experience(ruled_popped: Match) -> None:
    match = ready_all(ruled_popped)
    assert match.status == Status.PLAYER_DRAFT
    ping = OutboxEffect.objects.filter(kind="ping_lobby").order_by("-id").first()
    assert ping is not None and "Nobody here can captain yet" in ping.payload["text"]


def _allow(community: Community, *players: Player) -> None:
    for player in players:
        standing.set_override(community, player, "allow", None, "test")


def test_two_eligible_players_are_appointed_without_a_vote(
    captain_rule: Community, ruled_popped: Match, players: list[Player]
) -> None:
    _allow(captain_rule, players[4], players[9])
    match = ready_all(ruled_popped)
    assert match.status == Status.PLAYER_DRAFT
    captains = {captain_of(match, team).player_id for team in ("A", "B")}
    assert captains == {players[4].pk, players[9].pk}


def test_a_vote_for_an_ineligible_player_is_refused(
    captain_rule: Community, ruled_popped: Match, players: list[Player]
) -> None:
    _allow(captain_rule, *players[:3])
    match = ready_all(ruled_popped)
    assert match.status == Status.CAPTAIN_PHASE
    with pytest.raises(InvalidAction, match="can't captain yet"):
        lobby.cast_captain_votes(match.pk, players[5], [players[7].pk])
    lobby.cast_captain_votes(match.pk, players[5], [players[1].pk])


def test_a_barred_player_cannot_captain(
    captain_rule: Community, ruled_popped: Match, players: list[Player]
) -> None:
    _allow(captain_rule, *players[:3])
    standing.set_override(captain_rule, players[0], "bar", None, "troll picks")
    match = ready_all(ruled_popped)
    assert match.status == Status.PLAYER_DRAFT  # Only two left eligible, so no vote.
    captains = {captain_of(match, team).player_id for team in ("A", "B")}
    assert players[0].pk not in captains


def test_the_board_shows_who_can_captain(
    captain_rule: Community, ruled_popped: Match, players: list[Player]
) -> None:
    from league.services import boards

    _allow(captain_rule, *players[:3])
    match = ready_all(ruled_popped)
    state = boards.board_state(match.pk)
    assert state.captain_min_games == 5
    locked = [line for line in state.players if not line.captain_ok]
    assert len(locked) == 9
    assert all(line.captain_note == "0/5 matches" for line in locked)


def test_an_ineligible_sub_hands_the_captaincy_to_a_teammate(
    captain_rule: Community, ruled_popped: Match, players: list[Player]
) -> None:
    _allow(captain_rule, *players[:3])
    match = run_player_draft(vote_captains(ready_all(ruled_popped)))
    captain = captain_of(match, "A")
    teammate = (
        match.participants.filter(team="A", is_active=True, is_captain=False).order_by("id").first()
    )
    _allow(captain_rule, teammate.player)
    _sub_in(match, captain.player, 60)
    match = fresh(match)
    assert captain_of(match, "A").player_id == teammate.player_id
    sub_row = match.participants.get(player__discord_user_id=10_000_060)
    assert not sub_row.is_captain and sub_row.team == "A"


def test_completed_matches_count_and_a_void_takes_one_back(
    league: Community, players: list[Player]
) -> None:
    match = play_full_match(league, players, "A", 1)
    assert standing.completed_matches(league, [players[0].pk]) == {players[0].pk: 1}
    corrections.apply_outcome_correction(match.pk, "void", None, "test")
    assert standing.completed_matches(league, [players[0].pk]) == {players[0].pk: 0}


def test_five_completed_matches_make_a_player_eligible(
    captain_rule: Community, players: list[Player]
) -> None:
    for number in range(1, 6):
        play_full_match(captain_rule, players, "A", number)
    from league.domain.mode_schema import parse_mode

    mode = parse_mode(captain_rule.active_mode.body)
    rows = standing.eligibility(captain_rule, mode, [players[0].pk, make_player(99).pk])
    assert rows[players[0].pk].eligibility.ok
    assert rows[make_player(99).pk].eligibility.reason == "0/5 matches"
