"""The rating ledger stays exact through random histories, and results render the new parts."""

from __future__ import annotations

import random

import pytest

from bot.renderers.results import render_results, render_scoreboard_notice, team_table
from league.models import Community, Player, Season
from league.models.scoreboards import AwardSource
from league.services import boards, corrections, integrity
from league.services.common import captain_of
from league.services.community import start_season
from tests.helpers import play_full_match


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_histories_keep_the_ledger_equal_to_a_replay(
    seed: int, league: Community, players: list[Player]
) -> None:
    """Results, late awards, voids and rescores in a random order. The ledger, the projection
    and a replay from scratch must agree after every step."""
    season: Season = start_season(league, {"placement_games": 0})
    rng = random.Random(seed)
    played = []
    for number in range(1, 5):
        match = play_full_match(league, players, rng.choice("AB"), number)
        played.append(match)
        roster = list(
            match.participants.filter(removed_reason="").values_list("player_id", flat=True)
        )
        if rng.random() < 0.7:
            picked = rng.sample(roster, 3)
            awards = {picked[0]: "mvp", picked[1]: "key", picked[2]: "key"}
            corrections.apply_award_correction(match.pk, awards, AwardSource.STAFF, None, "VOD")
        assert integrity.check_season(season) == []
    target = rng.choice(played[:-1])
    corrections.apply_outcome_correction(target.pk, "void", None, "Wrong lobby")
    assert integrity.check_season(season) == []
    other = next(m for m in played if m.pk != target.pk)
    other.refresh_from_db()
    flipped = "B" if other.winner == "A" else "A"
    corrections.apply_outcome_correction(other.pk, flipped, None, "Misreported")
    assert integrity.check_season(season) == []


def test_results_show_awards_and_scoreboard_stats(league: Community, players: list[Player]) -> None:
    start_season(league, {"placement_games": 0})
    match = play_full_match(league, players, "A", 1)
    mvp = captain_of(match, "A").player
    corrections.apply_award_correction(match.pk, {mvp.pk: "mvp"}, AwardSource.STAFF, None, "x")
    model = boards.results_view(match.pk)
    assert "🏅" in team_table(model.team_a)
    payload = str(render_results(model, attachment_name=None).to_components())
    assert "won" in payload


def test_the_scoreboard_notice_has_a_match_names_button() -> None:
    payload = str(render_scoreboard_notice(7, 42, 2).to_components())
    assert "2 names need matching" in payload
    assert "dl:v1:ocr_map:7:42" in payload
