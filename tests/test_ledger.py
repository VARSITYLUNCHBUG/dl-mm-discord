"""Corrections, append-only guarantees and ledger invariants."""

from __future__ import annotations

import pytest
from django.db import DatabaseError, connection, transaction

from league.domain.engine import Status
from league.domain.errors import InvalidAction
from league.models import (
    Community,
    DraftAction,
    DraftModeVersion,
    Match,
    MatchResult,
    MatchTransition,
    PlayerRating,
    RatingEvent,
    RatingRun,
)
from league.services import corrections, integrity, lobby, modes, ratings
from tests.conftest import make_player
from tests.helpers import fresh, play_full_match, ready_all, to_in_progress, vote_captains

pytestmark = pytest.mark.django_db


def ladder(league: Community, count: int = 4) -> list[Match]:
    """Several matches with overlapping players, so a change must cascade."""
    pool = [make_player(n) for n in range(1, 17)]
    matches = []
    for index in range(count):
        rotated = pool[index * 2 :] + pool[: index * 2]
        winner = "A" if index % 2 == 0 else "B"
        matches.append(play_full_match(league, rotated[:12], winner, number=index + 1))
    return matches


def rating(season_owner: Match, player_id: int) -> int:
    return PlayerRating.objects.get(season=season_owner.season, player_id=player_id).rating


def test_ledger_is_clean_after_many_matches(league: Community) -> None:
    ladder(league)
    assert integrity.check_all() == []


def test_replay_from_scratch_reproduces_the_ledger(league: Community) -> None:
    matches = ladder(league)
    season = matches[0].season
    fold = corrections.fold_season(season)
    assert corrections.diff_against_ledger(season, fold) == {}


def test_void_cascades_and_stays_consistent(league: Community) -> None:
    matches = ladder(league)
    first = matches[0]
    corrections.apply_outcome_correction(first.pk, "void", actor=None, reason="Bug in lobby")
    match = fresh(first)
    assert match.status == Status.VOIDED
    assert match.winner == ""
    assert sum(RatingEvent.objects.filter(match=match).values_list("delta", flat=True)) == 0
    net = {}
    for event in RatingEvent.objects.filter(match=match):
        net[event.player_id] = net.get(event.player_id, 0) + event.delta
    assert set(net.values()) == {0}  # Every player's change from that match is gone.
    assert integrity.check_all() == []


def test_rescore_flips_result_and_stays_consistent(league: Community) -> None:
    matches = ladder(league)
    target = matches[1]
    assert target.winner == "B"
    corrections.apply_outcome_correction(target.pk, "A", actor=None, reason="Wrong report")
    assert fresh(target).winner == "A"
    assert MatchResult.objects.filter(match=target).count() == 2
    assert integrity.check_all() == []
    row = PlayerRating.objects.filter(season=target.season).first()
    assert row.wins + row.losses == row.games


def test_correction_is_append_only(league: Community) -> None:
    matches = ladder(league)
    before_ids = set(RatingEvent.objects.values_list("id", flat=True))
    before = dict(RatingEvent.objects.values_list("id", "delta"))
    corrections.apply_outcome_correction(matches[0].pk, "void", actor=None, reason="x")
    after = dict(RatingEvent.objects.filter(id__in=before_ids).values_list("id", "delta"))
    assert before == after


def test_preview_writes_nothing_and_predicts_the_result(league: Community) -> None:
    matches = ladder(league)
    target = matches[0]
    events_before = RatingEvent.objects.count()
    preview = corrections.preview_outcome(target, "void")
    assert RatingEvent.objects.count() == events_before
    predicted = {
        player_id: rating(target, player_id) + delta
        for player_id, delta in preview.net_by_player.items()
    }
    corrections.apply_outcome_correction(target.pk, "void", actor=None, reason="x")
    for player_id, expected in predicted.items():
        assert rating(target, player_id) == expected
    assert RatingEvent.objects.count() == events_before + preview.events_to_write


def test_void_then_restore_returns_to_original_ratings(league: Community) -> None:
    matches = ladder(league)
    target = matches[0]
    original = dict(
        PlayerRating.objects.filter(season=target.season).values_list("player_id", "rating")
    )
    corrections.apply_outcome_correction(target.pk, "void", actor=None, reason="oops")
    corrections.apply_outcome_correction(target.pk, "A", actor=None, reason="restored")
    match = fresh(target)
    assert match.status == Status.COMPLETED
    assert match.winner == "A"
    now_ratings = dict(
        PlayerRating.objects.filter(season=target.season).values_list("player_id", "rating")
    )
    assert now_ratings == original
    assert integrity.check_all() == []


def test_correcting_to_same_winner_is_refused(league: Community) -> None:
    match = ladder(league, 1)[0]
    with pytest.raises(InvalidAction, match="already the recorded winner"):
        corrections.apply_outcome_correction(match.pk, "A", actor=None, reason="x")


def test_live_match_cannot_use_correction_path(popped: Match) -> None:
    with pytest.raises(InvalidAction, match="finished"):
        corrections.apply_outcome_correction(popped.pk, "void", actor=None, reason="x")


def test_match_voided_before_teams_cannot_be_rated(popped: Match) -> None:
    lobby.staff_void(popped.pk, None, "Called off")
    with pytest.raises(InvalidAction, match="before teams were drafted"):
        corrections.apply_outcome_correction(popped.pk, "A", actor=None, reason="x")


def test_match_voided_during_the_player_draft_cannot_be_rated(popped: Match) -> None:
    # Captains already have a team by then, so a check for any team member isn't enough.
    match = vote_captains(ready_all(popped))
    lobby.staff_void(match.pk, None, "Called off")
    with pytest.raises(InvalidAction, match="before teams were drafted"):
        corrections.apply_outcome_correction(match.pk, "A", actor=None, reason="x")
    assert RatingEvent.objects.count() == 0


def test_late_sub_moves_result_to_the_substitute(league: Community) -> None:
    match = ladder(league, 2)[0]
    leaving = match.participants.filter(team="A").first().player
    sub = make_player(200)
    preview = corrections.preview_sub(fresh(match), leaving, sub)
    assert sub.pk in preview.net_by_player
    corrections.apply_sub_correction(match.pk, leaving, sub, actor=None, reason="Wrong player")
    assert RatingEvent.objects.filter(match=match, player=sub).exists()
    net = sum(
        RatingEvent.objects.filter(match=match, player=leaving).values_list("delta", flat=True)
    )
    assert net == 0
    assert integrity.check_all() == []


def test_seed_and_adjust_survive_a_correction(league: Community) -> None:
    matches = ladder(league)
    season = matches[0].season
    player = make_player(1)
    ratings.seed_player(season, player, 1500, actor=None)
    ratings.adjust_player(season, make_player(2), -25, actor=None, reason="Conduct")
    corrections.apply_outcome_correction(matches[0].pk, "void", actor=None, reason="x")
    assert integrity.check_all() == []
    kinds = set(RatingRun.objects.values_list("kind", flat=True))
    assert {"seed", "admin", "correction", "live"} <= kinds


def test_seed_before_play_affects_expected_score(league: Community) -> None:
    season = league.seasons.get()
    pool = [make_player(n) for n in range(1, 13)]
    for player in pool[:6]:
        ratings.seed_player(season, player, 1500, actor=None)
    match = play_full_match(league, pool, "A", 1)
    assert integrity.check_all() == []
    assert RatingEvent.objects.filter(match=match, expected__isnull=False).exists()


def test_rebuild_repairs_a_corrupted_projection(league: Community) -> None:
    match = ladder(league, 2)[0]
    PlayerRating.objects.filter(season=match.season).update(rating=1, games=0)
    assert integrity.check_all() != []
    corrections.rebuild_projection(match.season)
    assert integrity.check_all() == []
    row = PlayerRating.objects.filter(season=match.season).first()
    assert row.games > 0


def test_integrity_detects_a_non_zero_sum_run(league: Community) -> None:
    match = ladder(league, 1)[0]
    run = RatingRun.objects.filter(match=match).first()
    RatingEvent.objects.create(
        run=run,
        season=match.season,
        player=make_player(99),
        delta=5,
        rating_after=1005,
        created_at=run.created_at,
    )
    assert any("sums to 5" in problem for problem in integrity.check_all())


# --- Append-only triggers ------------------------------------------------------------------


@pytest.mark.parametrize(
    "model", [RatingEvent, RatingRun, MatchResult, MatchTransition, DraftAction, DraftModeVersion]
)
def test_history_rows_cannot_be_updated_or_deleted(league: Community, model: type) -> None:
    ladder(league, 1)
    row = model.objects.first()
    assert row is not None
    # Raw SQL: this tests the database's own guard, not Django's PROTECT check in front of it.
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(f"DELETE FROM {model._meta.db_table} WHERE id = %s", [row.pk])
    field = {
        RatingEvent: "delta",
        RatingRun: "reason",
        MatchResult: "reason",
        MatchTransition: "to_status",
        DraftAction: "source",
        DraftModeVersion: "name",
    }[model]
    value = 1 if field == "delta" else "tampered"
    with pytest.raises(DatabaseError, match="append-only"), transaction.atomic():
        model.objects.filter(pk=row.pk).update(**{field: value})


# --- Modes ---------------------------------------------------------------------------------


def test_republishing_identical_mode_keeps_version(league: Community) -> None:
    active = league.active_mode
    assert modes.publish(active.body).pk == active.pk


def test_editing_a_mode_makes_a_new_version(league: Community) -> None:
    active = league.active_mode
    body = {**active.body, "name": "Renamed"}
    new = modes.publish(body)
    assert new.version == active.version + 1
    assert new.pk != active.pk


def test_live_match_keeps_pinned_mode_after_activation(popped: Match) -> None:
    body = dict(popped.mode_version.body)
    body["ready_check"] = {"seconds": 60, "backfill": "next_in_queue"}
    new = modes.publish(body)
    modes.activate(popped.community_id, new.pk)
    match = fresh(popped)
    assert match.mode_version_id != new.pk
    assert match.mode_version.body["ready_check"]["seconds"] == 180
    # New lobbies pick up the new version.
    community = Community.objects.get(pk=popped.community_id)
    assert community.active_mode_id == new.pk


def test_alternative_mode_runs_end_to_end(league: Community) -> None:
    three_ban = DraftModeVersion.objects.get(slug="threeban_1win_2026")
    modes.activate(league.pk, three_ban.pk)
    pool = [make_player(n) for n in range(1, 13)]
    match = play_full_match(league, pool, "A", 1)
    assert match.status == Status.COMPLETED
    assert DraftAction.objects.filter(match=match, action="ban").count() == 6
    assert DraftAction.objects.filter(match=match, action="pick").count() == 12


def test_in_progress_helper_reaches_expected_state(popped: Match) -> None:
    assert to_in_progress(popped).status == Status.IN_PROGRESS
