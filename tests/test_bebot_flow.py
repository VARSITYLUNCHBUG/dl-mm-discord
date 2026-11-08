"""The Bebot flow (bebot_2ban): captain vote, side claim, Hidden King first, Random and the GLHF."""

from __future__ import annotations

from typing import Any

import pytest

from bot.renderers.board import render_board, render_picker
from bot.routing import registered_actions
from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import CaptainCandidate, Status, draft_name, tally_captain_votes
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.domain.mode_schema import ModeValidationError, parse_mode
from league.models import (
    CaptainVote,
    Champion,
    Community,
    DraftAction,
    DraftModeVersion,
    Match,
    Player,
)
from league.services import boards, champions, deadlines, draft, lobby, modes, queue, subs
from league.services.common import captain_of, mode_of
from tests.conftest import Clock, make_player
from tests.helpers import claim_sides, fresh, keep_captains, ready_all, run_player_draft
from tests.test_bot import all_buttons, all_custom_ids, assert_within_limits
from tests.test_domain import PRESETS

pytestmark = pytest.mark.django_db

BEBOT = modes.load_yaml(PRESETS / "bebot_2ban.yaml")


@pytest.fixture
def bebot(league: Community) -> Community:
    version = DraftModeVersion.objects.filter(slug="bebot_2ban").order_by("-version").first()
    modes.activate(league.pk, version.pk)
    league.refresh_from_db()
    return league


@pytest.fixture
def voting(bebot: Community, players: list[Player]) -> Match:
    """A Bebot-flow lobby where everyone is ready and the captain vote is open."""
    for player in players:
        queue.join(bebot.pk, player)
    return ready_all(Match.objects.get())


def acting_captain(match: Match) -> Player:
    step = build_plan(mode_of(match))[match.step_index]
    return captain_of(match, team_for_role(step.role, match.champion_first_pick)).player


def at_side_select(voting: Match) -> Match:
    return run_player_draft(keep_captains(voting))


def at_champion_draft(voting: Match) -> Match:
    return claim_sides(at_side_select(voting))


def set_rating(match: Match, player: Player, rating: int) -> None:
    from league.services import ratings

    ratings.seed_player(match.season, player, rating, None)


# --- Rules ---------------------------------------------------------------------------------


def test_preset_parses_with_bebot_rules() -> None:
    mode = parse_mode(BEBOT)
    assert mode.captains.selection == "vote"
    assert mode.champion_draft.priority == "hidden_king_first"
    assert mode.champion_draft.random_button
    assert mode.champion_draft.on_timeout.ban == "auto_random"
    assert mode.champion_draft.seconds == 40
    dcs = modes.load_yaml(PRESETS / "dcs_s3_2ban.yaml")
    assert BEBOT["champion_draft"]["steps"] == dcs["champion_draft"]["steps"]  # type: ignore[index]


def test_a_vote_with_hand_offs_is_rejected() -> None:
    captains: dict[str, Any] = dict(BEBOT["captains"])  # type: ignore[arg-type]
    captains["handoff"] = {"requests_per_captain": 2, "accept_seconds": 30}
    with pytest.raises(ModeValidationError, match="voted in"):
        parse_mode({**BEBOT, "captains": captains})


CANDIDATES = [CaptainCandidate(pid, rating, 5) for pid, rating in [(1, 1000), (2, 1200), (3, 1100)]]


def test_tally_ranks_by_votes_first() -> None:
    assert tally_captain_votes({1: 5, 3: 4, 2: 1}, CANDIDATES, seed=7) == (1, 3)


def test_tally_breaks_a_tie_on_elo() -> None:
    assert tally_captain_votes({1: 3, 3: 3, 2: 3}, CANDIDATES, seed=7) == (2, 3)


def test_tally_with_no_votes_takes_the_top_two_by_elo() -> None:
    assert tally_captain_votes({}, CANDIDATES, seed=7) == (2, 3)


def test_tally_fills_a_short_vote_with_elo() -> None:
    assert tally_captain_votes({1: 1}, CANDIDATES, seed=7) == (1, 2)


def test_tally_is_reproducible_on_a_full_tie() -> None:
    level = [CaptainCandidate(pid, 1000, 0) for pid in range(1, 7)]
    first = tally_captain_votes({}, level, seed=42)
    assert all(tally_captain_votes({}, level, seed=42) == first for _ in range(5))


def test_draft_name_is_stable_per_seed() -> None:
    assert draft_name(123) == draft_name(123)
    assert len(draft_name(123).split()) == 2


# --- Captain vote --------------------------------------------------------------------------


def test_ready_lobby_opens_a_vote_with_no_captains(voting: Match) -> None:
    assert voting.status == Status.CAPTAIN_PHASE
    assert not voting.participants.filter(is_captain=True).exists()
    opened = voting.transitions.get(to_status=Status.CAPTAIN_PHASE).created_at
    assert 44 <= (voting.deadline_at - opened).total_seconds() <= 46


def test_voting_again_replaces_your_votes(voting: Match, players: list[Player]) -> None:
    lobby.cast_captain_votes(voting.pk, players[0], [players[1].pk, players[2].pk])
    lobby.cast_captain_votes(voting.pk, players[0], [players[3].pk])
    votes = CaptainVote.objects.filter(match=voting, voter=players[0])
    assert list(votes.values_list("candidate_id", flat=True)) == [players[3].pk]


def test_you_can_vote_for_yourself(voting: Match, players: list[Player]) -> None:
    chosen = lobby.cast_captain_votes(voting.pk, players[0], [players[0].pk])
    assert chosen == [players[0]]


def test_three_votes_are_refused(voting: Match, players: list[Player]) -> None:
    with pytest.raises(InvalidAction, match="up to 2"):
        lobby.cast_captain_votes(voting.pk, players[0], [p.pk for p in players[1:4]])


def test_voting_for_someone_outside_the_lobby_is_refused(
    voting: Match, players: list[Player]
) -> None:
    outsider = make_player(99)
    with pytest.raises(InvalidAction, match="in this lobby"):
        lobby.cast_captain_votes(voting.pk, players[0], [outsider.pk])


def test_someone_outside_the_lobby_cannot_vote(voting: Match, players: list[Player]) -> None:
    with pytest.raises(InvalidAction, match="not in this match"):
        lobby.cast_captain_votes(voting.pk, make_player(99), [players[0].pk])


def test_last_vote_closes_the_vote_and_starts_the_player_draft(
    voting: Match, players: list[Player]
) -> None:
    for voter in players:
        lobby.cast_captain_votes(voting.pk, voter, [players[4].pk, players[7].pk])
    match = fresh(voting)
    assert match.status == Status.PLAYER_DRAFT
    captains = set(match.participants.filter(is_captain=True).values_list("player_id", flat=True))
    assert captains == {players[4].pk, players[7].pk}
    assert not match.handoffs.exists()
    with pytest.raises(StaleAction):
        lobby.cast_captain_votes(match.pk, players[0], [players[1].pk])


def test_vote_timeout_counts_votes_and_elo_fills_the_gap(
    voting: Match, players: list[Player], clock: Clock
) -> None:
    set_rating(voting, players[9], 1500)
    lobby.cast_captain_votes(voting.pk, players[0], [players[2].pk])
    lobby.cast_captain_votes(voting.pk, players[1], [players[2].pk])
    clock.advance(46)
    deadlines.tick()
    match = fresh(voting)
    assert match.status == Status.PLAYER_DRAFT
    assert captain_of(match, "A").player_id == players[2].pk
    assert captain_of(match, "B").player_id == players[9].pk


def test_vote_result_is_announced_after_the_transition(voting: Match) -> None:
    from league.models import OutboxEffect

    match = keep_captains(voting)
    kinds = list(OutboxEffect.objects.filter(match=match).values_list("kind", flat=True))
    announce = max(i for i, kind in enumerate(kinds) if kind == "ping_lobby")
    cleared = max(i for i, kind in enumerate(kinds) if kind == "clear_notices")
    assert announce > cleared


# --- Side claim ----------------------------------------------------------------------------


def test_player_draft_ends_at_the_side_claim(voting: Match) -> None:
    match = at_side_select(voting)
    assert match.status == Status.SIDE_SELECT
    assert match.hidden_king_team == ""


def test_first_claim_decides_both_sides_and_hidden_king_goes_first(voting: Match) -> None:
    match = at_side_select(voting)
    draft.claim_side(match.pk, captain_of(match, "B").player, "hk")
    match = fresh(match)
    assert match.status == Status.CHAMPION_DRAFT
    assert match.hidden_king_team == "B"
    assert match.champion_first_pick == "B"
    with pytest.raises(StaleAction):
        draft.claim_side(match.pk, captain_of(match, "A").player, "hk")


def test_claiming_archmother_gives_the_other_team_hidden_king(voting: Match) -> None:
    match = at_side_select(voting)
    draft.claim_side(match.pk, captain_of(match, "B").player, "am")
    match = fresh(match)
    assert match.hidden_king_team == "A"
    assert match.champion_first_pick == "A"


def test_only_a_captain_can_claim_a_side(voting: Match) -> None:
    match = at_side_select(voting)
    player = match.participants.filter(is_captain=False).first().player
    with pytest.raises(NotAllowed):
        draft.claim_side(match.pk, player, "hk")


def test_side_claim_timeout_flips_a_coin(voting: Match, clock: Clock) -> None:
    match = at_side_select(voting)
    clock.advance(21)
    deadlines.tick()
    match = fresh(match)
    assert match.status == Status.CHAMPION_DRAFT
    assert match.hidden_king_team in {"A", "B"}
    assert match.champion_first_pick == match.hidden_king_team


# --- Champion draft ------------------------------------------------------------------------


def test_bebot_draft_runs_16_steps_with_hidden_king_banning_first(voting: Match) -> None:
    match = at_champion_draft(voting)
    hidden_king = match.hidden_king_team
    first_turn = (draft.available_champions(match)[0], acting_captain(match))
    assert captain_of(match, hidden_king).player == first_turn[1]
    while fresh(match).status == Status.CHAMPION_DRAFT:
        match = fresh(match)
        draft.choose_champion(
            match.pk, acting_captain(match), match.step_index, draft.available_champions(match)[0]
        )
    match = fresh(match)
    assert match.status == Status.LOBBY_CODE
    actions = DraftAction.objects.filter(match=match).exclude(action="player_pick")
    assert actions.count() == 16
    assert actions.filter(action="ban").count() == 4
    assert actions.order_by("step_index").first().team == hidden_king


def test_a_timed_out_ban_takes_a_random_champion(voting: Match, clock: Clock) -> None:
    match = at_champion_draft(voting)
    for _ in range(16):
        clock.advance(41)
        deadlines.tick()
    match = fresh(match)
    assert match.status == Status.LOBBY_CODE
    bans = DraftAction.objects.filter(match=match, action="ban")
    assert bans.count() == 4
    assert all(ban.target_champion_id is not None for ban in bans)
    assert all(ban.source == "timeout" for ban in bans)


def test_random_button_takes_an_available_champion(voting: Match) -> None:
    match = at_champion_draft(voting)
    available = draft.available_champions(match)
    champion = draft.choose_random_champion(match.pk, acting_captain(match), match.step_index)
    assert champion in available
    action = DraftAction.objects.get(match=match, step_index=match.step_index)
    assert action.random_choice
    assert action.source == "user"
    assert action.target_champion_id == champion
    assert fresh(match).step_index == match.step_index + 1


def test_random_button_refuses_the_wrong_captain_and_a_stale_step(voting: Match) -> None:
    match = at_champion_draft(voting)
    waiting = captain_of(match, "B" if match.champion_first_pick == "A" else "A").player
    with pytest.raises(NotAllowed):
        draft.choose_random_champion(match.pk, waiting, match.step_index)
    draft.choose_random_champion(match.pk, acting_captain(match), match.step_index)
    with pytest.raises(StaleAction):
        draft.choose_random_champion(match.pk, acting_captain(fresh(match)), match.step_index)


def test_random_button_is_refused_in_formats_without_it(popped: Match) -> None:
    match = run_player_draft(keep_captains(ready_all(popped)))
    assert match.status == Status.CHAMPION_DRAFT
    with pytest.raises(InvalidAction, match="Random"):
        draft.choose_random_champion(match.pk, acting_captain(match), match.step_index)


# --- Subs ----------------------------------------------------------------------------------


def _sub_in(match: Match, leaving: Player, number: int) -> Player:
    request = subs.request_sub_out(match.pk, leaving, leaving.pk)
    target = make_player(number)
    offer = subs.invite_sub(request.pk, leaving, target.discord_user_id, target.display_name)
    subs.answer_offer(offer.pk, target, True)
    return target


def test_a_sub_during_the_vote_drops_the_leavers_votes(
    voting: Match, players: list[Player]
) -> None:
    lobby.cast_captain_votes(voting.pk, players[3], [players[5].pk])
    lobby.cast_captain_votes(voting.pk, players[0], [players[3].pk])
    sub = _sub_in(voting, players[3], 60)
    match = fresh(voting)
    assert match.status == Status.CAPTAIN_PHASE  # The sub hasn't voted, so the vote stays open.
    assert not CaptainVote.objects.filter(match=match, voter=players[3]).exists()
    assert not CaptainVote.objects.filter(match=match, candidate=players[3]).exists()
    lobby.cast_captain_votes(match.pk, sub, [sub.pk])


def test_a_sub_during_the_side_claim_resumes_it(voting: Match) -> None:
    match = at_side_select(voting)
    leaving = match.participants.filter(is_captain=False, is_active=True).first().player
    _sub_in(match, leaving, 61)
    match = fresh(match)
    assert match.status == Status.SIDE_SELECT
    assert match.deadline_at is not None
    draft.claim_side(match.pk, captain_of(match, "A").player, "hk")
    assert fresh(match).status == Status.CHAMPION_DRAFT


# --- Board ---------------------------------------------------------------------------------


def _views(match: Match) -> list[Any]:
    state = boards.board_state(match.pk)
    views = [render_board(state)]
    picker = render_picker(state)
    if picker is not None:
        views.append(picker)
    return views


def _check(match: Match) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for view in _views(match):
        assert_within_limits(view)
        payload = view.to_components()
        for custom_id in all_custom_ids(payload):
            assert custom_id.split(":")[2] in registered_actions(), custom_id
        components.extend(payload)
    return components


def test_vote_board_has_a_two_choice_select(voting: Match, players: list[Player]) -> None:
    lobby.cast_captain_votes(voting.pk, players[0], [players[1].pk])
    payload = _check(voting)
    text = str(payload)
    assert "1/12 voted" in text
    assert "captain_vote" in text
    assert "'max_values': 2" in text


def test_side_board_has_both_claim_buttons(voting: Match) -> None:
    labels = [button.get("label") for button in all_buttons(_check(at_side_select(voting)))]
    assert "Become Hidden King captain" in labels
    assert "Become Archmother captain" in labels


def test_champion_board_has_one_random_button(voting: Match) -> None:
    labels = [button.get("label") for button in all_buttons(_check(at_champion_draft(voting)))]
    assert labels.count("Random") == 1


def test_random_choice_is_marked_on_the_board(voting: Match) -> None:
    match = at_champion_draft(voting)
    draft.choose_random_champion(match.pk, acting_captain(match), match.step_index)
    assert "🎲" in str(_check(fresh(match)))


def test_finished_draft_shows_its_name_gif_and_glhf(voting: Match) -> None:
    match = at_champion_draft(voting)
    for _ in range(16):
        match = fresh(match)
        draft.choose_random_champion(match.pk, acting_captain(match), match.step_index)
    match = fresh(match)
    assert match.status == Status.LOBBY_CODE
    text = str(_check(match))
    assert draft_name(match.rng_seed) in text
    assert "GLHF" in text
    assert "tenor.com" in text  # The GIF, from presets/draft_gifs.json.


def test_boards_with_four_pending_subs_stay_within_limits(
    voting: Match, players: list[Player]
) -> None:
    match = at_side_select(voting)
    for leaving in match.participants.filter(is_captain=False, is_active=True)[:4]:
        subs.request_sub_out(match.pk, leaving.player, leaving.player_id)
    _check(fresh(match))


# --- Emojis --------------------------------------------------------------------------------


def test_bebot_emojis_load_onto_champions(league: Community) -> None:
    result = champions.load_emojis(PRESETS / "champion_emojis.yaml")
    assert not result.unknown
    assert Champion.objects.get(name="Abrams").discord_emoji_id == 1302512783283191879
    again = champions.load_emojis(PRESETS / "champion_emojis.yaml")
    assert not again.updated
