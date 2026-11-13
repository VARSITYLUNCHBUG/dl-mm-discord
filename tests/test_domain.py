"""Pure rules. No database."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import (
    CaptainCandidate,
    DraftablePlayer,
    Status,
    auto_champion,
    auto_player_pick,
    can_transition,
    draft_name,
    dry_run,
    first_pick_team,
    tally_captain_votes,
)
from league.domain.mode_schema import DraftMode, ModeValidationError, parse_mode

PRESETS = Path(__file__).resolve().parent.parent / "presets"
THREE_BAN = yaml.safe_load((PRESETS / "threeban_1win_2026.yaml").read_text(encoding="utf-8"))
DCS = yaml.safe_load((PRESETS / "dcs_s3_2ban.yaml").read_text(encoding="utf-8"))
BEBOT = yaml.safe_load((PRESETS / "bebot_2ban.yaml").read_text(encoding="utf-8"))


def body(**changes: object) -> dict[str, object]:
    return {**THREE_BAN, **changes}


# --- Mode schema ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [BEBOT, THREE_BAN, DCS])
def test_presets_parse(raw: dict[str, object]) -> None:
    assert isinstance(parse_mode(raw), DraftMode)


def test_bebot_preset_is_dcs_order_at_40_seconds() -> None:
    mode = parse_mode(BEBOT)
    assert mode.captains.vote_seconds == 45
    assert mode.champion_draft.side_seconds == 20
    assert mode.champion_draft.seconds == 40
    assert BEBOT["champion_draft"]["steps"] == DCS["champion_draft"]["steps"]


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("captains", "selection", "top_rating"),
        ("captains", "handoff", {"requests_per_captain": 2, "accept_seconds": 30}),
        ("captains", "first_pick", "coin_flip"),
        ("champion_draft", "priority", "coin_flip"),
        ("champion_draft", "random_button", False),
        ("champion_draft", "on_timeout", {"ban": "skip", "pick": "auto_random"}),
    ],
)
def test_the_flow_itself_is_not_configurable(section: str, key: str, value: object) -> None:
    changed = {**BEBOT[section], key: value}
    with pytest.raises(ModeValidationError) as caught:
        parse_mode({**BEBOT, section: changed})
    assert f"{section}.{key}" in str(caught.value)


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


CANDIDATES = candidates((1, 1000, 5), (2, 1200, 5), (3, 1100, 5))


def test_tally_ranks_by_votes_first() -> None:
    assert tally_captain_votes({1: 5, 3: 4, 2: 1}, CANDIDATES, seed=7) == (1, 3)


def test_tally_breaks_a_tie_on_elo() -> None:
    assert tally_captain_votes({1: 3, 3: 3, 2: 3}, CANDIDATES, seed=7) == (2, 3)


def test_tally_with_no_votes_takes_the_top_two_by_elo() -> None:
    assert tally_captain_votes({}, CANDIDATES, seed=7) == (2, 3)


def test_tally_fills_a_short_vote_with_elo() -> None:
    assert tally_captain_votes({1: 1}, CANDIDATES, seed=7) == (1, 2)


def test_tally_rating_tie_goes_to_more_games() -> None:
    pool = candidates((1, 1000, 5), (2, 1000, 50), (3, 1000, 20))
    assert tally_captain_votes({}, pool, seed=1) == (2, 3)


def test_tally_full_tie_is_seeded_and_reproducible() -> None:
    pool = candidates(*[(i, 1000, 0) for i in range(1, 13)])
    first = tally_captain_votes({}, pool, seed=7)
    assert first == tally_captain_votes({}, list(reversed(pool)), seed=7)
    results = {tally_captain_votes({}, pool, seed=s) for s in range(20)}
    assert len(results) > 1  # Launch-day ties aren't always the same two people.


def test_draft_name_is_stable_per_seed() -> None:
    assert draft_name(123) == draft_name(123)
    assert len(draft_name(123).split()) == 2


def test_lower_rated_captain_picks_first() -> None:
    assert first_pick_team(1500, 1400, seed=1) == "B"
    assert first_pick_team(1300, 1400, seed=1) == "A"
    assert first_pick_team(1400, 1400, seed=1) == first_pick_team(1400, 1400, seed=1)
    assert {first_pick_team(1400, 1400, seed=s) for s in range(20)} == {"A", "B"}


def test_auto_pick_takes_highest_rated() -> None:
    pool = [DraftablePlayer(1, 900), DraftablePlayer(2, 1500), DraftablePlayer(3, 1200)]
    assert auto_player_pick(pool, "auto_highest_rating", 1, 0) == 2


def test_auto_champion_is_reproducible_and_legal() -> None:
    assert auto_champion([3, 5, 9], 42, 12) == auto_champion([9, 3, 5], 42, 12)
    assert auto_champion([3, 5, 9], 42, 12) in {3, 5, 9}


def test_transitions_forbid_skipping_phases() -> None:
    assert can_transition(Status.READY_CHECK, Status.CAPTAIN_PHASE)
    assert not can_transition(Status.READY_CHECK, Status.PLAYER_DRAFT)
    assert not can_transition(Status.PLAYER_DRAFT, Status.CHAMPION_DRAFT)
    assert can_transition(Status.PLAYER_DRAFT, Status.SIDE_SELECT)
    assert not can_transition(Status.IN_PROGRESS, Status.COMPLETED)
    assert can_transition(Status.AWAITING_CONFIRMATION, Status.COMPLETED)


def test_team_for_role() -> None:
    assert team_for_role("first", "B") == "B"
    assert team_for_role("second", "B") == "A"
