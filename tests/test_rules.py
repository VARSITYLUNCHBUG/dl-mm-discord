"""MVP-farming freezes, early-leave voids, the left-early flag and the award check."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed
from league.domain.openskill_v1 import farming_frozen, merged_params
from league.models import (
    Community,
    EarlyLeave,
    Match,
    OutboxEffect,
    Penalty,
    Player,
    QueueEntry,
    RatingEvent,
    Season,
)
from league.models.matches import EarlyLeaveStatus
from league.models.moderation import PenaltyKind
from league.models.scoreboards import AwardSource
from league.services import (
    api_results,
    calibration,
    corrections,
    early_leave,
    integrity,
    queue,
    results,
    steam,
)
from league.services.common import captain_of
from league.services.community import start_season
from tests.conftest import Clock, make_player
from tests.helpers import fresh, image, play_full_match, to_in_progress

PARAMS = merged_params(None)


# --- MVP farming -----------------------------------------------------------------------------


def test_farming_freezes_only_a_rate_well_above_chance_over_enough_games() -> None:
    awards = {1: "mvp", 2: "key", 3: "mvp"}
    history = {1: (20, 4), 2: (20, 3), 3: (19, 10)}  # 20%, 15%, and too few games.
    assert farming_frozen(awards, history, PARAMS) == frozenset({1})
    assert farming_frozen(awards, {1: (20, 3)}, PARAMS) == frozenset()  # 15% is under 16.7%.


@pytest.fixture
def farm_season(league: Community) -> Season:
    """Awards count at once, and three matches are enough to judge a rate."""
    return start_season(league, {"placement_games": 0, "farming_min_games": 3})


def _play_with_mvp(league: Community, players: list[Player], number: int, mvp: Player) -> Match:
    for player in players:
        queue.join(league.pk, player)
    match = to_in_progress(Match.objects.filter(community=league).order_by("-id").first())
    team = match.participants.get(player=mvp).team
    results.submit_report(
        match.pk, captain_of(match, team).player, team, image(number), None, {mvp.pk: "mvp"}
    )
    results.confirm(match.pk, captain_of(match, "B" if team == "A" else "A").player)
    return fresh(match)


def test_an_mvp_every_game_freezes_the_award_share_live_and_on_replay(
    farm_season: Season, league: Community, players: list[Player]
) -> None:
    farmer = players[5]
    matches = [_play_with_mvp(league, players, n, farmer) for n in range(1, 6)]
    events = {m.pk: RatingEvent.objects.get(match=m, player=farmer) for m in matches}
    # Three MVPs in three games: from the fourth match the share is frozen.
    assert all(events[m.pk].award_adj for m in matches[:3])
    assert not any(events[m.pk].award_adj for m in matches[3:])
    assert integrity.check_season(farm_season) == []  # The replay agrees.
    pings = OutboxEffect.objects.filter(kind="ping_staff", payload__text__icontains="froze")
    assert pings.count() == 1
    report = calibration.report(farm_season)
    assert report.farming and report.farming[0][0] == farmer.display_name
    assert report.frozen_matches == 2


def test_a_late_award_change_refolds_the_freeze(
    farm_season: Season, league: Community, players: list[Player]
) -> None:
    farmer = players[5]
    matches = [_play_with_mvp(league, players, n, farmer) for n in range(1, 5)]
    assert farmer.pk in corrections.fold_season(farm_season).frozen[matches[3].pk]
    # Staff move the first three MVPs to someone else: the farmer's fourth award counts again.
    for match in matches[:3]:
        corrections.apply_award_correction(
            match.pk, {players[0].pk: "mvp"}, AwardSource.STAFF, None, "Wrong MVP"
        )
    assert integrity.check_season(farm_season) == []
    frozen = corrections.fold_season(farm_season).frozen
    assert farmer.pk not in frozen.get(matches[3].pk, frozenset())
    assert RatingEvent.objects.filter(match=matches[3], player=farmer, award_adj__isnull=False)


# --- Early leaves ----------------------------------------------------------------------------


@pytest.fixture
def game_on(league: Community, players: list[Player]) -> Match:
    for player in players:
        queue.join(league.pk, player)
    return to_in_progress(Match.objects.get())


def _leaver(match: Match) -> Player:
    return match.participants.filter(team="A", is_captain=False).order_by("id").first().player


def test_an_agreed_early_leave_voids_the_match_and_penalises_the_leaver(game_on: Match) -> None:
    leaver = _leaver(game_on)
    claim = early_leave.report(game_on.pk, captain_of(game_on, "A").player, leaver.pk)
    assert fresh(game_on).status == Status.IN_PROGRESS
    from league.services import boards

    assert boards.board_state(game_on.pk).early_leave_name == leaver.display_name
    with pytest.raises(NotAllowed):
        early_leave.answer(game_on.pk, captain_of(game_on, "A").player, True)
    early_leave.answer(game_on.pk, captain_of(game_on, "B").player, True)
    match = fresh(game_on)
    assert match.status == Status.VOIDED
    assert not RatingEvent.objects.filter(match=match).exists()
    assert Penalty.objects.get(player=leaver).kind == PenaltyKind.LEAVER
    assert match.participants.get(player=leaver).left_early
    assert EarlyLeave.objects.get(pk=claim.pk).status == EarlyLeaveStatus.APPROVED
    queued = set(QueueEntry.objects.values_list("player_id", flat=True))
    assert leaver.pk not in queued and len(queued) == 11


def test_a_disagreed_early_leave_goes_to_staff(game_on: Match) -> None:
    early_leave.report(game_on.pk, captain_of(game_on, "A").player, _leaver(game_on).pk)
    early_leave.answer(game_on.pk, captain_of(game_on, "B").player, False)
    assert fresh(game_on).status == Status.IN_PROGRESS
    assert OutboxEffect.objects.filter(
        kind="ping_staff", payload__text__icontains="leave early"
    ).exists()


def test_an_unanswered_early_leave_goes_to_staff(game_on: Match, clock: Clock) -> None:
    early_leave.report(game_on.pk, captain_of(game_on, "A").player, _leaver(game_on).pk)
    clock.advance(121)
    assert early_leave.tick(clock.now()) == 1
    assert EarlyLeave.objects.get().status == EarlyLeaveStatus.EXPIRED


def test_early_leaves_must_be_reported_early(game_on: Match, clock: Clock) -> None:
    clock.advance(21 * 60)
    with pytest.raises(InvalidAction, match="too late"):
        early_leave.report(game_on.pk, captain_of(game_on, "A").player, _leaver(game_on).pk)


def test_staff_can_void_an_early_leave_after_a_report(game_on: Match) -> None:
    leaver = _leaver(game_on)
    results.submit_report(game_on.pk, captain_of(game_on, "A").player, "A", image(1))
    early_leave.staff_void(game_on.pk, leaver, None)
    assert fresh(game_on).status == Status.VOIDED
    assert fresh(game_on).participants.get(player=leaver).left_early


def test_subbing_out_mid_game_marks_the_leaver(game_on: Match) -> None:
    from league.services import subs

    leaver = _leaver(game_on)
    request = subs.request_sub_out(game_on.pk, leaver, leaver.pk)
    sub = make_player(60)
    offer = subs.invite_sub(request.pk, leaver, sub.discord_user_id, sub.display_name)
    subs.answer_offer(offer.pk, sub, True)
    assert fresh(game_on).participants.get(player=leaver).left_early


# --- deadlock-api abandons -------------------------------------------------------------------


def test_an_early_abandon_in_deadlock_api_flags_the_player(game_on: Match) -> None:
    data: dict[str, Any] = json.loads(
        (Path(__file__).parent / "fixtures" / "deadlock_api_sample.json").read_text()
    )
    data = copy.deepcopy(data)
    hk = game_on.hidden_king_team
    by_team = {0: [], 1: []}
    for row in data["match_info"]["players"]:
        by_team[row["team"]].append(row)
    hk_rows = list(game_on.participants.filter(team=hk).order_by("id"))
    for participant, api_player in zip(hk_rows, by_team[0], strict=True):
        steam.link(participant.player, api_player["account_id"])
    by_team[0][0]["abandon_match_time_s"] = 140
    results.submit_report(game_on.pk, captain_of(game_on, hk).player, hk, image(1), 900000001)
    api_results.record_metadata(game_on.pk, 900000001, data)
    assert fresh(game_on).participants.get(player=hk_rows[0].player).left_early
    assert OutboxEffect.objects.filter(kind="ping_staff", payload__text__icontains="2:20").exists()


# --- The award check -------------------------------------------------------------------------


def test_award_holders_who_keep_winning_show_a_signal(
    farm_season: Season, league: Community, players: list[Player]
) -> None:
    # Players 0-5 are strong: their team always wins, and player 0 is always MVP. Rate "farming"
    # off so the award stays in play.
    Season.objects.filter(pk=farm_season.pk).update(
        rating_params={"placement_games": 0, "farming_min_games": 1000}
    )
    for number in range(1, 9):
        for player in players:
            queue.join(league.pk, player)
        match = to_in_progress(Match.objects.filter(community=league).order_by("-id").first())
        team = match.participants.get(player=players[0]).team
        results.submit_report(
            match.pk, captain_of(match, team).player, team, image(number), None,
            {players[0].pk: "mvp"},
        )  # fmt: skip
        results.confirm(match.pk, captain_of(match, "B" if team == "A" else "A").player)
    report = calibration.report(Season.objects.get(pk=farm_season.pk))
    signal = report.awards_signal
    assert signal is not None and signal.holders is not None and signal.others is not None
    assert signal.holders.n > 0 and signal.others.n > 0


@pytest.fixture
def staff_client(league: Community) -> Any:
    from django.contrib.auth import get_user_model
    from django.test import Client

    client = Client()
    client.force_login(get_user_model().objects.create_user("staff-rules", is_staff=True))
    return client


def test_the_dashboard_shows_the_award_check(
    staff_client: Any, league: Community, players: list[Player]
) -> None:
    for number in range(1, 3):
        play_full_match(league, players, "A", number)
    body = staff_client.get("/admin/calibration/").content.decode()
    assert "Do MVP and key-player awards mean anything?" in body
    assert "Award share frozen" in body


def test_early_leave_window_starts_when_the_bot_lobby_starts(game_on: Match, clock: Clock) -> None:
    Match.objects.filter(pk=game_on.pk).update(api_started_at=clock.now() + timedelta(minutes=15))
    clock.advance(30 * 60)  # 30 min after the code, 15 after the start.
    assert early_leave.can_report(fresh(game_on))
