"""OpenSkill seasons: the algorithm's invariants, and the ledger staying exact through the flow."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from league.domain.openskill_v1 import (
    DEFAULT_PARAMS,
    OpenSkillAlgo,
    display,
    grown_sigma,
    merged_params,
)
from league.domain.ratings_algo import MatchInput, PlayerState, d6
from league.models import (
    Community,
    MatchAward,
    OutboxEffect,
    Player,
    PlayerRating,
    RatingEvent,
    RatingRun,
    Season,
)
from league.models.ratings import RunKind
from league.models.scoreboards import AwardSource
from league.services import awards, corrections, integrity, ratings, results
from league.services.common import captain_of
from league.services.community import start_season
from tests.helpers import fresh, image, play_full_match, to_in_progress

ALGO = OpenSkillAlgo()
PARAMS = merged_params(None)
WHEN = datetime(2026, 6, 1, tzinfo=UTC)


def _state(mu: float, sigma: float, last: datetime | None = None, games: int = 20) -> PlayerState:
    mu_d, sigma_d = d6(mu), d6(sigma)
    return PlayerState(display(mu_d, sigma_d, PARAMS), mu_d, sigma_d, games, last)


def _match(awards_: dict[int, str] | None = None, outcome: str = "A") -> MatchInput:
    return MatchInput(tuple(range(6)), tuple(range(6, 12)), outcome, awards_ or {}, WHEN)


team_states = st.lists(st.tuples(st.floats(5, 45), st.floats(2, 8.4)), min_size=12, max_size=12)


@settings(max_examples=60, deadline=None)
@given(team_states, st.sampled_from(["A", "B"]), st.integers(0, 11), st.integers(0, 11))
def test_awards_are_zero_sum_within_each_team_and_capped(
    raw: list[tuple[float, float]], outcome: str, mvp: int, key: int
) -> None:
    states = {i: _state(mu, sigma) for i, (mu, sigma) in enumerate(raw)}
    awards_ = {key: "key", mvp: "mvp"}
    changes = ALGO.rate(states, _match(awards_, outcome), PARAMS)
    for team in (range(6), range(6, 12)):
        assert sum((changes[p].award_adj or Decimal(0)) for p in team) == 0
    for change in changes.values():
        if change.award_adj is not None:
            assert abs(change.award_adj) <= Decimal("0.500001")


def test_awards_wait_until_placement_is_over() -> None:
    # Placement counts games: sigma stays high for a long time in 6v6, so it can't be the switch.
    placing = {i: _state(25, 8.33, games=9) for i in range(12)}
    changes = ALGO.rate(placing, _match({0: "mvp"}), PARAMS)
    assert all(change.award_adj is None for change in changes.values())
    placed = {i: _state(25, 8.33, games=10) for i in range(12)}
    assert ALGO.rate(placed, _match({0: "mvp"}), PARAMS)[0].award_adj is not None


def test_the_mvp_gains_more_than_a_teammate_on_a_win() -> None:
    states = {i: _state(25, 4.0) for i in range(12)}
    changes = ALGO.rate(states, _match({0: "mvp", 1: "key"}), PARAMS)
    assert changes[0].mu_delta > changes[1].mu_delta > changes[2].mu_delta > 0


def test_the_mvp_loses_less_on_a_loss() -> None:
    states = {i: _state(25, 4.0) for i in range(12)}
    changes = ALGO.rate(states, _match({0: "mvp"}, outcome="B"), PARAMS)
    assert changes[2].mu_delta < changes[0].mu_delta < 0


def test_placement_moves_are_capped() -> None:
    # One-player teams take the whole update, so an upset moves them past the cap.
    states = {0: _state(40, 8.33, games=0), 1: _state(10, 8.33, games=0)}
    changes = ALGO.rate(states, MatchInput((0,), (1,), "B", {}, WHEN), PARAMS)
    assert all(abs(change.mu_delta) == 3 for change in changes.values())
    assert all(change.cap_applied for change in changes.values())
    # The same upset after placement isn't capped.
    placed = {0: _state(40, 8.33, games=10), 1: _state(10, 8.33, games=10)}
    changes = ALGO.rate(placed, MatchInput((0,), (1,), "B", {}, WHEN), PARAMS)
    assert all(abs(change.mu_delta) > 3 and not change.cap_applied for change in changes.values())


def test_sigma_never_drops_below_the_floor() -> None:
    states = {i: _state(25, 2.0) for i in range(12)}
    changes = ALGO.rate(states, _match(), PARAMS)
    for player_id, change in changes.items():
        assert states[player_id].sigma + change.sigma_delta >= Decimal("2.0")


def test_rating_is_deterministic() -> None:
    rng = random.Random(4)
    states = {i: _state(rng.uniform(10, 40), rng.uniform(2, 8)) for i in range(12)}
    first = ALGO.rate(states, _match({3: "mvp"}), PARAMS)
    second = ALGO.rate(states, _match({3: "mvp"}), PARAMS)
    assert first == second


def test_inactivity_grows_sigma_but_never_past_the_cap_or_down() -> None:
    # 0.5 in quadrature per full 14 days: slow, and capped at 6.
    assert grown_sigma(_state(25, 3.0, WHEN - timedelta(days=28)), WHEN, PARAMS) == d6(9.5**0.5)
    long_ago = WHEN - timedelta(days=3000)
    assert grown_sigma(_state(25, 3.0, long_ago), WHEN, PARAMS) == Decimal("6.000000")
    assert grown_sigma(_state(25, 3.0, WHEN - timedelta(days=13)), WHEN, PARAMS) == d6(3.0)
    assert grown_sigma(_state(25, 8.0, long_ago), WHEN, PARAMS) == d6(8.0)


def test_a_new_player_shows_zero_and_seeds_by_band() -> None:
    start = ALGO.initial(PARAMS)
    assert start.rating == 0
    high = ALGO.seed(start, "high", PARAMS)
    assert high.mu_delta == Decimal(10)
    with pytest.raises(ValueError):
        ALGO.seed(start, "legendary", PARAMS)


# --- Through the flow ------------------------------------------------------------------------


@pytest.fixture
def openskill(league: Community) -> Season:
    """A season with no placement, so awards apply from the first match."""
    return start_season(league, {"placement_games": 0})


def test_a_new_season_rates_with_openskill(
    openskill: Season, league: Community, players: list[Player]
) -> None:
    assert openskill.start_rating == 0  # mu 25 - 3 x 8.33 rounds to nothing.
    match = play_full_match(league, players, "A", 1)
    run = RatingRun.objects.get(match=match, kind=RunKind.LIVE)
    assert run.algo_version == "openskill_pl_v1"
    row = PlayerRating.objects.get(season=openskill, player=captain_of(match, "A").player)
    assert row.mu > Decimal(25) and row.sigma < Decimal(25) / 3
    assert integrity.check_season(openskill) == []


def test_report_awards_feed_the_rating(
    openskill: Season, league: Community, players: list[Player]
) -> None:
    for player in players:
        from league.services import queue

        queue.join(league.pk, player)
    match = to_in_progress(Season.objects.get(pk=openskill.pk).matches.get())
    first = captain_of(match, "A").player
    team_a = [p.player_id for p in match.participants.filter(team="A")]
    team_b = [p.player_id for p in match.participants.filter(team="B")]
    picks = {team_a[1]: "mvp", team_b[0]: "key"}
    results.submit_report(match.pk, first, "A", image(1), None, picks)
    results.confirm(match.pk, captain_of(match, "B").player)
    events = {e.player_id: e for e in RatingEvent.objects.filter(match=match)}
    assert events[team_a[1]].award_adj > 0
    assert events[team_b[0]].award_adj > 0  # A smaller loss.
    assert sum(events[p].award_adj or 0 for p in team_a) == 0
    assert integrity.check_season(openskill) == []


def test_staff_awards_after_the_result_correct_the_ledger(
    openskill: Season, league: Community, players: list[Player]
) -> None:
    match = play_full_match(league, players, "A", 1)
    winner = captain_of(match, "A").player
    run = corrections.apply_award_correction(
        match.pk, {winner.pk: "mvp"}, AwardSource.STAFF, None, "Watched the VOD"
    )
    assert run is not None and run.kind == RunKind.CORRECTION
    assert OutboxEffect.objects.filter(kind="refresh_results", match=match).exists()
    assert integrity.check_season(openskill) == []
    assert ratings.current_awards(fresh(match)) == {winner.pk: "mvp"}
    # And the results view shows the corrected numbers and the mark.
    from league.services import boards

    view = boards.results_view(match.pk)
    row = next(r for r in view.team_a + view.team_b if r.player_id == winner.pk)
    assert row.award == "mvp"
    assert row.delta == ratings.match_deltas(match)[winner.pk]


def test_staff_awards_beat_the_scoreboard_and_reports(
    openskill: Season, league: Community, players: list[Player]
) -> None:
    match = play_full_match(league, players, "A", 1)
    first, second = captain_of(match, "A").player, captain_of(match, "B").player
    assert awards.record(match, {first.pk: "mvp"}, AwardSource.STAFF, None) is not None
    assert awards.record(match, {second.pk: "mvp"}, AwardSource.OCR, None) is None
    assert awards.record(match, {second.pk: "mvp"}, AwardSource.REPORT, second) is None
    assert ratings.current_awards(match) == {first.pk: "mvp"}
    assert MatchAward.objects.filter(match=match).count() == 1


def test_awards_must_go_to_the_roster(
    openskill: Season, league: Community, players: list[Player]
) -> None:
    from league.domain.errors import InvalidAction
    from tests.conftest import make_player

    match = play_full_match(league, players, "A", 1)
    with pytest.raises(InvalidAction):
        awards.record(match, {make_player(70).pk: "mvp"}, AwardSource.STAFF, None)
    first, second = captain_of(match, "A").player, captain_of(match, "B").player
    with pytest.raises(InvalidAction):
        awards.record(match, {first.pk: "mvp", second.pk: "mvp"}, AwardSource.STAFF, None)


def test_a_void_after_awards_refolds_cleanly(
    openskill: Season, league: Community, players: list[Player]
) -> None:
    first = play_full_match(league, players, "A", 1)
    play_full_match(league, players, "B", 2)
    corrections.apply_award_correction(
        first.pk, {captain_of(first, "A").player_id: "mvp"}, AwardSource.STAFF, None, "x"
    )
    corrections.apply_outcome_correction(first.pk, "void", None, "Wrong lobby")
    assert integrity.check_season(openskill) == []
    fold = corrections.fold_season(openskill)
    assert first.pk not in fold.expected_a


def test_seeding_a_band_and_the_strength_used_for_captains(
    openskill: Season, players: list[Player]
) -> None:
    from league.services.common import strength_of

    event = ratings.seed_player(openskill, players[0], "high", None)
    assert event.mu_delta == Decimal(10)
    assert strength_of(openskill, players[0].pk) == 35_000
    assert strength_of(openskill, players[1].pk) == 25_000
    assert integrity.check_season(openskill) == []


def test_the_board_shows_win_chance_and_placement(
    openskill: Season, league: Community, players: list[Player]
) -> None:
    from league.services import boards, queue
    from tests.helpers import ready_all as ready
    from tests.helpers import run_player_draft, vote_captains

    season = start_season(league)  # Default sigma, so everyone is placing.
    for player in players:
        queue.join(league.pk, player)
    match = run_player_draft(vote_captains(ready(season.matches.get())))
    state = boards.board_state(match.pk)
    assert state.win_chance_a == pytest.approx(0.5)
    assert all(line.rating_label == "P 0/10" for line in state.players)


def test_default_params_are_the_documented_starting_values() -> None:
    assert DEFAULT_PARAMS["beta"] == pytest.approx(25 / 6)
    assert DEFAULT_PARAMS["seeds"] == {"new": 15.0, "mid": 25.0, "high": 35.0}
