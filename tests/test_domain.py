"""Pure rules. No database."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from league.domain import elo
from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import (
    CaptainCandidate,
    DraftablePlayer,
    Status,
    auto_champion,
    auto_player_pick,
    can_transition,
    dry_run,
    first_pick_team,
    select_captains,
)
from league.domain.mode_schema import DraftMode, ModeValidationError, parse_mode

PRESETS = Path(__file__).resolve().parent.parent / "presets"
THREE_BAN = yaml.safe_load((PRESETS / "threeban_1win_2026.yaml").read_text(encoding="utf-8"))
DCS = yaml.safe_load((PRESETS / "dcs_s3_2ban.yaml").read_text(encoding="utf-8"))


def body(**changes: object) -> dict[str, object]:
    return {**THREE_BAN, **changes}


# --- Elo -----------------------------------------------------------------------------------


def test_report_worked_example_a_wins() -> None:
    team_a = [1420, 1310, 1250, 1200, 1120, 1050]
    team_b = [1390, 1280, 1230, 1190, 1140, 1090]
    result = elo.compute(team_a, team_b, a_won=True, k=32)
    assert result.avg_a == 1225
    assert result.avg_b == 1220
    assert round(result.expected_a, 3) == 0.507
    assert result.delta_a == 16


def test_report_worked_example_b_wins() -> None:
    team_a = [1420, 1310, 1250, 1200, 1120, 1050]
    team_b = [1390, 1280, 1230, 1190, 1140, 1090]
    assert elo.compute(team_a, team_b, a_won=False, k=32).delta_a == -16


@pytest.mark.parametrize(
    ("gap", "win", "loss"),
    [(0, 16, -16), (50, 14, -18), (100, 12, -20), (200, 8, -24), (400, 3, -29)],
)
def test_stakes_table_from_report(gap: int, win: int, loss: int) -> None:
    favourite = [1000 + gap] * 6
    underdog = [1000] * 6
    assert elo.compute(favourite, underdog, a_won=True, k=32).delta_a == win
    assert elo.compute(favourite, underdog, a_won=False, k=32).delta_a == loss


def test_rounding_is_half_up_not_bankers() -> None:
    # K * (S - E) lands exactly on 8.5: Python's round() would give 8 here.
    result = elo.compute([1000], [1000], a_won=True, k=17)
    assert result.delta_a == 9  # 17 * 0.5 = 8.5, rounds up
    assert round(8.5) == 8


@given(
    st.lists(st.integers(0, 4000), min_size=6, max_size=6),
    st.lists(st.integers(0, 4000), min_size=6, max_size=6),
    st.booleans(),
    st.integers(1, 100),
)
def test_elo_is_exactly_zero_sum(a: list[int], b: list[int], a_won: bool, k: int) -> None:
    delta = elo.compute(a, b, a_won, k).delta_a
    total = sum(r + delta for r in a) + sum(r - delta for r in b)
    assert total == sum(a) + sum(b)


@given(
    st.lists(st.integers(0, 4000), min_size=6, max_size=6),
    st.lists(st.integers(0, 4000), min_size=6, max_size=6),
)
def test_winner_never_loses_points(a: list[int], b: list[int]) -> None:
    assert elo.compute(a, b, True, 32).delta_a >= 0
    assert elo.compute(a, b, False, 32).delta_a <= 0


# --- Mode schema ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [THREE_BAN, DCS])
def test_presets_parse(raw: dict[str, object]) -> None:
    assert isinstance(parse_mode(raw), DraftMode)


def test_three_ban_expands_to_exact_1win_table() -> None:
    plan = build_plan(parse_mode(THREE_BAN))
    champion_steps = [(step.action, step.role) for step in plan[10:]]
    assert champion_steps == [
        ("ban", "first"),
        ("ban", "second"),
        ("ban", "first"),
        ("ban", "second"),
        ("pick", "first"),
        ("pick", "second"),
        ("pick", "second"),
        ("pick", "first"),
        ("pick", "second"),
        ("pick", "first"),
        ("ban", "second"),
        ("ban", "first"),
        ("pick", "second"),
        ("pick", "first"),
        ("pick", "first"),
        ("pick", "second"),
        ("pick", "first"),
        ("pick", "second"),
    ]


def test_dcs_preset_matches_rulebook_16_steps() -> None:
    plan = build_plan(parse_mode(DCS))
    champion_steps = "".join(
        ("b" if step.action == "ban" else "p") + step.role[0].upper() for step in plan[10:]
    )
    assert champion_steps == "bFbSpFpSpSpFpFpSbSbFpSpFpFpSpSpF"
    assert len(plan[10:]) == 16


def test_ready_check_default_is_180_seconds() -> None:
    assert parse_mode(THREE_BAN).ready_check.seconds == 180


def test_wrong_player_order_has_readable_error() -> None:
    raw = body(player_draft={"order": "FSFS", "seconds": 45})
    with pytest.raises(ModeValidationError) as caught:
        parse_mode(raw)
    assert "each captain must make 5 picks; got F=2, S=2" in str(caught.value)


def test_missing_champion_pick_has_readable_error() -> None:
    steps = list(THREE_BAN["champion_draft"]["steps"])[:-1]
    raw = body(champion_draft={**THREE_BAN["champion_draft"], "steps": steps})
    with pytest.raises(ModeValidationError) as caught:
        parse_mode(raw)
    assert "second team has 5 champion picks, needs 6" in str(caught.value)


def test_unknown_fields_rejected_with_path() -> None:
    raw = body(typo_field=1)
    with pytest.raises(ModeValidationError) as caught:
        parse_mode(raw)
    assert "typo_field" in str(caught.value)


def test_blind_steps_rejected_until_engine_supports_them() -> None:
    steps = [
        {"action": "ban", "team": "both", "hidden": True},
        *THREE_BAN["champion_draft"]["steps"],
    ]
    with pytest.raises(ModeValidationError):
        parse_mode(body(champion_draft={**THREE_BAN["champion_draft"], "steps": steps}))


def test_dry_run_rejects_pool_too_small() -> None:
    with pytest.raises(ModeValidationError) as caught:
        dry_run(parse_mode(THREE_BAN), list(range(10)))
    assert "needs 18 champions" in str(caught.value)


def test_dry_run_counts_exclusions_against_the_pool() -> None:
    draft = {**THREE_BAN["champion_draft"], "excluded_champion_ids": [0, 1]}
    with pytest.raises(ModeValidationError):
        dry_run(parse_mode(body(champion_draft=draft)), list(range(19)))


# --- Engine --------------------------------------------------------------------------------


def candidates(*rows: tuple[int, int, int]) -> list[CaptainCandidate]:
    return [CaptainCandidate(*row) for row in rows]


def test_highest_rated_become_captains() -> None:
    pool = candidates((1, 1200, 10), (2, 1400, 10), (3, 1300, 10), (4, 1000, 10))
    assert select_captains(pool, "top_rating", 0, seed=1) == (2, 3)


def test_rating_tie_goes_to_more_games() -> None:
    pool = candidates((1, 1000, 5), (2, 1000, 50), (3, 1000, 20))
    assert select_captains(pool, "top_rating", 0, seed=1) == (2, 3)


def test_full_tie_is_seeded_and_reproducible() -> None:
    pool = candidates(*[(i, 1000, 0) for i in range(1, 13)])
    first = select_captains(pool, "top_rating", 0, seed=7)
    assert first == select_captains(list(reversed(pool)), "top_rating", 0, seed=7)
    results = {select_captains(pool, "top_rating", 0, seed=s) for s in range(20)}
    assert len(results) > 1  # Launch-day ties aren't always the same two people.


def test_min_games_filters_and_falls_back_to_random() -> None:
    pool = candidates((1, 2000, 0), (2, 1100, 20), (3, 1000, 20))
    assert select_captains(pool, "top_rating", 10, seed=1) == (2, 3)
    few = candidates((1, 2000, 0), (2, 1100, 0), (3, 1000, 20))
    chosen = select_captains(few, "top_rating", 10, seed=1)
    assert len(set(chosen)) == 2


def test_lower_rated_captain_picks_first() -> None:
    assert first_pick_team("lower_rated_captain", 1500, 1400, seed=1) == "B"
    assert first_pick_team("lower_rated_captain", 1300, 1400, seed=1) == "A"
    assert first_pick_team("higher_rated_captain", 1500, 1400, seed=1) == "A"


def test_auto_pick_takes_highest_rated() -> None:
    pool = [DraftablePlayer(1, 900), DraftablePlayer(2, 1500), DraftablePlayer(3, 1200)]
    assert auto_player_pick(pool, "auto_highest_rating", 1, 0) == 2


def test_auto_champion_is_reproducible_and_legal() -> None:
    assert auto_champion([3, 5, 9], 42, 12) == auto_champion([9, 3, 5], 42, 12)
    assert auto_champion([3, 5, 9], 42, 12) in {3, 5, 9}


def test_transitions_forbid_skipping_phases() -> None:
    assert can_transition(Status.READY_CHECK, Status.CAPTAIN_PHASE)
    assert not can_transition(Status.READY_CHECK, Status.PLAYER_DRAFT)
    assert not can_transition(Status.IN_PROGRESS, Status.COMPLETED)
    assert can_transition(Status.AWAITING_CONFIRMATION, Status.COMPLETED)


def test_team_for_role() -> None:
    assert team_for_role("first", "B") == "B"
    assert team_for_role("second", "B") == "A"
