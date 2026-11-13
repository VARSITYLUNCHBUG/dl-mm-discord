"""Steam links, result checks against deadlock-api, bot-hosted lobbies and the dashboard.

The API itself is never called: client tests use a mock transport, and service tests feed in a
trimmed copy of a real custom match's metadata, with the match and Steam account IDs made up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.test import Client

from league.domain import steam as steam_ids
from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.integrations import deadlock_api
from league.models import ApiMatchCheck, Community, Match, OutboxEffect, Player
from league.models.api import CheckStatus
from league.services import api_results, draft, integrity, queue, ratings, results, steam
from league.services.common import captain_of
from tests.conftest import make_player
from tests.helpers import (
    fresh,
    image,
    run_champion_draft,
    to_champion_draft,
    to_in_progress,
)

FIXTURE = Path(__file__).parent / "fixtures" / "deadlock_api_sample.json"
DEADLOCK_ID = 900000001


def metadata() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def accounts_by_team() -> dict[int, list[int]]:
    teams: dict[int, list[int]] = {0: [], 1: []}
    for player in metadata()["match_info"]["players"]:
        teams[player["team"]].append(player["account_id"])
    return teams


# --- Steam IDs -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "account"),
    [
        ("100000004", 100000004),
        ("76561198060265732", 100000004),
        ("https://steamcommunity.com/profiles/76561198060265732/", 100000004),
        ("[U:1:100000004]", 100000004),
        ("STEAM_0:0:50000002", 100000004),
    ],
)
def test_steam_ids_parse_every_common_form(raw: str, account: int) -> None:
    ref = steam_ids.parse(raw)
    assert ref is not None and ref.account_id == account


def test_custom_urls_need_resolving_and_junk_is_refused() -> None:
    ref = steam_ids.parse("https://steamcommunity.com/id/vexa")
    assert ref is not None and ref.vanity == "vexa" and ref.account_id is None
    assert steam_ids.parse("not a steam id") is None
    assert steam_ids.parse("0") is None
    assert steam_ids.steam64_from_account(100000004) == 76561198060265732


def test_a_steam_account_links_to_one_player_only(league: Community) -> None:
    first, second = make_player(1), make_player(2)
    steam.link(first, 100000004, "Vexa")
    with pytest.raises(InvalidAction, match="already linked"):
        steam.link(second, 100000004)
    assert steam.unlink(first)
    steam.link(second, 100000004)
    assert Player.objects.get(pk=second.pk).steam_account_id == 100000004


def test_relinking_another_account_clears_verification(league: Community) -> None:
    player = make_player(1)
    steam.link(player, 1)
    steam.mark_verified([player.pk])
    steam.link(player, 2)
    assert Player.objects.get(pk=player.pk).steam_verified_at is None


# --- Client ----------------------------------------------------------------------------------


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Routes deadlock-api requests to canned responses. Set routes[(method, path)]."""
    routes: dict[tuple[str, str], tuple[int, Any]] = {}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        status, body = routes.get((request.method, request.url.path), (404, {"error": "nope"}))
        return httpx.Response(status, json=body)

    def client() -> httpx.Client:
        return httpx.Client(
            base_url="https://api.test", transport=httpx.MockTransport(handler), headers={}
        )

    monkeypatch.setattr(deadlock_api, "_client", client)
    return {"routes": routes, "seen": seen}


def test_client_creates_and_starts_a_lobby(mock_api: dict[str, Any]) -> None:
    mock_api["routes"][("POST", "/v1/matches/custom/create")] = (
        200,
        {"party_id": "1234", "party_code": "ABCD-EF", "callback_secret": None},
    )
    mock_api["routes"][("POST", "/v1/matches/custom/1234/start")] = (200, {})
    lobby = deadlock_api.create_custom_match("south_africa")
    assert lobby == deadlock_api.CreatedLobby("1234", "ABCD-EF")
    assert json.loads(mock_api["seen"][0].content)["server_region"] == "south_africa"
    deadlock_api.start_custom_match("1234")


def test_client_maps_errors_and_misses(mock_api: dict[str, Any]) -> None:
    mock_api["routes"][("POST", "/v1/matches/custom/create")] = (429, {"error": "slow down"})
    with pytest.raises(deadlock_api.ApiError) as error:
        deadlock_api.create_custom_match()
    assert error.value.rate_limited
    assert deadlock_api.match_metadata(1) is None  # 404: not ingested yet.
    assert deadlock_api.custom_match_id("1234") is None
    mock_api["routes"][("GET", "/v1/matches/custom/1234/match-id")] = (200, {"match_id": 77})
    assert deadlock_api.custom_match_id("1234") == 77


def test_client_reads_steam_names(mock_api: dict[str, Any]) -> None:
    mock_api["routes"][("GET", "/v1/players/steam")] = (
        200,
        [{"account_id": 100000004, "personaname": "Vexa"}],
    )
    assert deadlock_api.steam_names([100000004]) == {100000004: "Vexa"}


# --- Result checks ---------------------------------------------------------------------------


@pytest.fixture
def live(league: Community, players: list[Player]) -> Match:
    """A game in progress whose players are linked to the fixture match's accounts."""
    for player in players:
        queue.join(league.pk, player)
    match = to_in_progress(Match.objects.get())
    hk = match.hidden_king_team
    other = "B" if hk == "A" else "A"
    teams = accounts_by_team()
    for team, accounts in ((hk, teams[0]), (other, teams[1])):
        rows = match.participants.filter(team=team).order_by("id")
        for row, account in zip(rows, accounts, strict=True):
            steam.link(row.player, account)
    return fresh(match)


def _player_for(account: int) -> Player:
    return Player.objects.get(steam_account_id=account)


def _report(match: Match, winner: str, deadlock_id: int | None = DEADLOCK_ID) -> None:
    results.submit_report(match.pk, captain_of(match, winner).player, winner, image(1), deadlock_id)


def test_a_report_with_a_match_id_queues_a_check(live: Match) -> None:
    _report(live, live.hidden_king_team)
    assert fresh(live).deadlock_match_id == DEADLOCK_ID
    effect = OutboxEffect.objects.get(kind="check_match_api")
    assert effect.payload == {"attempt": 1, "deadlock_match_id": DEADLOCK_ID}


def test_an_agreeing_match_verifies_links_and_sets_awards(live: Match) -> None:
    hk = live.hidden_king_team
    _report(live, hk)
    check = api_results.record_metadata(live.pk, DEADLOCK_ID, metadata())
    assert check.status == CheckStatus.OK and check.api_winner == hk
    assert check.wrong_team == [] and check.missing == []
    assert Player.objects.filter(steam_verified_at__isnull=False).count() == 12
    assert ratings.current_awards(live) == {
        _player_for(100000004).pk: "mvp",
        _player_for(100000103).pk: "key",
        _player_for(100000106).pk: "key",
    }
    results.confirm(live.pk, captain_of(live, "B" if hk == "A" else "A").player)
    assert integrity.check_all() == []


def test_the_other_winner_disputes_an_unconfirmed_report(live: Match) -> None:
    loser = "B" if live.hidden_king_team == "A" else "A"
    _report(live, loser)
    check = api_results.record_metadata(live.pk, DEADLOCK_ID, metadata())
    assert check.status == CheckStatus.MISMATCH
    assert fresh(live).status == Status.DISPUTED
    assert ratings.current_awards(live) == {}  # A mismatch never sets awards.


def test_a_linked_player_on_the_wrong_team_goes_to_staff(live: Match) -> None:
    hk = live.hidden_king_team
    # Swap two players' links across the teams.
    a = live.participants.filter(team=hk).order_by("id").first().player
    b = live.participants.exclude(team=hk).order_by("id").first().player
    a_account, b_account = a.steam_account_id, b.steam_account_id
    steam.unlink(a)
    steam.unlink(b)
    steam.link(a, b_account)
    steam.link(b, a_account)
    _report(live, hk)
    check = api_results.record_metadata(live.pk, DEADLOCK_ID, metadata())
    assert check.status == CheckStatus.MISMATCH
    assert set(check.wrong_team) == {a.pk, b.pk}
    assert OutboxEffect.objects.filter(
        kind="ping_staff", payload__text__icontains="other team"
    ).exists()
    assert fresh(live).status == Status.AWAITING_CONFIRMATION


def test_a_miss_is_retried_with_backoff_then_given_up(live: Match) -> None:
    _report(live, live.hidden_king_team)
    for attempt in range(1, len(api_results.BACKOFF_MINUTES) + 1):
        api_results.record_miss(live.pk, DEADLOCK_ID, CheckStatus.NOT_FOUND, 404, "")
        assert ApiMatchCheck.objects.filter(match=live).count() == attempt
    queued = OutboxEffect.objects.filter(kind="check_match_api").count()
    assert queued == len(api_results.BACKOFF_MINUTES)  # The first, then one per retry left.


def test_checks_stay_off_when_the_server_turns_them_off(league: Community, live: Match) -> None:
    Community.objects.filter(pk=league.pk).update(api_results=False)
    _report(fresh(live), live.hidden_king_team)
    assert not OutboxEffect.objects.filter(kind="check_match_api").exists()


def test_results_show_api_stats_for_linked_players(live: Match) -> None:
    from league.services import boards

    hk = live.hidden_king_team
    _report(live, hk)
    api_results.record_metadata(live.pk, DEADLOCK_ID, metadata())
    view = boards.results_view(live.pk)
    rows = {row.player_id: row for row in view.team_a + view.team_b}
    assert "12/8/13" in rows[_player_for(100000004).pk].stat_line
    assert view.deadlock_match_id == DEADLOCK_ID and view.duration_s == 2210


# --- Bot-hosted lobbies ----------------------------------------------------------------------


@pytest.fixture
def auto(league: Community) -> Community:
    Community.objects.filter(pk=league.pk).update(auto_lobby=True, lobby_region="south_africa")
    league.refresh_from_db()
    return league


def _to_lobby_code(league: Community, players: list[Player]) -> Match:
    for player in players:
        queue.join(league.pk, player)
    match = run_champion_draft(to_champion_draft(Match.objects.get()))
    assert match.status == Status.LOBBY_CODE
    return match


def test_auto_lobby_asks_the_api_and_starts_the_game(
    auto: Community, players: list[Player]
) -> None:
    match = _to_lobby_code(auto, players)
    assert OutboxEffect.objects.filter(kind="create_api_lobby", match=match).exists()
    assert draft.accept_api_lobby(match.pk, "1234", "ABCD-EF")
    match = fresh(match)
    assert match.status == Status.IN_PROGRESS
    assert match.lobby_code == "ABCD-EF" and match.api_party_id == "1234"
    from league.services import boards

    state = boards.board_state(match.pk)
    assert state.api_lobby and not state.api_started and state.api_leaves_at is not None


def test_a_failed_api_lobby_hands_it_back_to_the_host(
    auto: Community, players: list[Player]
) -> None:
    match = _to_lobby_code(auto, players)
    draft.api_lobby_failed(match.pk, "deadlock-api 500")
    ping = OutboxEffect.objects.filter(kind="ping", match=match).order_by("-id").first()
    assert ping is not None and "couldn't make the lobby" in ping.payload["text"]
    assert fresh(match).status == Status.LOBBY_CODE
    # A code entered by hand afterwards wins, and a late API answer is ignored.
    draft.submit_lobby_code(match.pk, fresh(match).lobby_code_host, "MANUAL1")
    assert not draft.accept_api_lobby(match.pk, "1234", "ABCD-EF")
    assert fresh(match).lobby_code == "MANUAL1"


def test_only_a_captain_starts_the_api_lobby_and_only_once(
    auto: Community, players: list[Player]
) -> None:
    match = _to_lobby_code(auto, players)
    draft.accept_api_lobby(match.pk, "1234", "ABCD-EF")
    match = fresh(match)
    outsider = match.participants.filter(is_captain=False).first().player
    with pytest.raises(NotAllowed):
        draft.check_can_start_api(match.pk, outsider)
    captain = captain_of(match, "A").player
    assert draft.check_can_start_api(match.pk, captain) == "1234"
    draft.mark_api_started(match.pk, captain)
    with pytest.raises(StaleAction):
        draft.check_can_start_api(match.pk, captain)
    assert OutboxEffect.objects.filter(kind="fetch_api_match_id", match=match).exists()


def test_the_started_lobby_match_id_is_kept_and_checked_after_the_report(
    auto: Community, players: list[Player]
) -> None:
    match = _to_lobby_code(auto, players)
    draft.accept_api_lobby(match.pk, "1234", "ABCD-EF")
    draft.mark_api_started(match.pk, captain_of(fresh(match), "A").player)
    draft.record_api_match_id(match.pk, None, 1)  # Not known yet: tries again.
    assert OutboxEffect.objects.filter(kind="fetch_api_match_id", payload__attempt=2).exists()
    draft.record_api_match_id(match.pk, DEADLOCK_ID, 2)
    match = fresh(match)
    assert match.deadlock_match_id == DEADLOCK_ID
    assert not OutboxEffect.objects.filter(kind="check_match_api").exists()  # Game still on.
    _report(match, match.hidden_king_team, None)
    assert OutboxEffect.objects.filter(kind="check_match_api").exists()


# --- Dashboard -------------------------------------------------------------------------------


@pytest.fixture
def staff_client(league: Community) -> Client:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user("staff-test", is_staff=True)
    client = Client()
    client.force_login(user)
    return client


def test_the_dashboard_needs_staff(league: Community) -> None:
    response = Client().get("/admin/calibration/")
    assert response.status_code == 302 and "/admin/login/" in response["Location"]


def test_the_dashboard_shows_the_season(staff_client: Client, live: Match) -> None:
    hk = live.hidden_king_team
    _report(live, hk)
    results.confirm(live.pk, captain_of(live, "B" if hk == "A" else "A").player)
    response = staff_client.get("/admin/calibration/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "Brier score" in body and "<svg" in body and "data-tip=" in body
    compared = staff_client.get("/admin/calibration/", {"params": '{"beta": 3.5}'})
    assert "Candidate parameters" in compared.content.decode()
    refused = staff_client.get("/admin/calibration/", {"params": '{"nope": 1}'})
    assert "Unknown parameters: nope" in refused.content.decode()
