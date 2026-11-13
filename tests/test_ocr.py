"""Score OCR in the flow: names matched to players, stats stored, awards and cross-checks.

These feed the reader's output straight in, from the real scoreboard fixtures, so they need no
OCR dependencies. One test runs the real engine and skips without the `ocr` group.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from league.domain.engine import Status
from league.domain.errors import InvalidAction
from league.models import (
    Community,
    Match,
    OutboxEffect,
    Player,
    PlayerAlias,
    RatingEvent,
    RatingRun,
    ResultSubmission,
    ScoreboardRead,
    Season,
)
from league.models.ratings import RunKind
from league.models.scoreboards import AwardSource, ReadStatus
from league.services import awards, integrity, ocr, queue, ratings, results
from league.services.common import captain_of
from league.services.community import start_season
from tests.helpers import fresh, image, to_in_progress

FIXTURES = Path(__file__).parent / "fixtures"
HK = ["Nevermore", "AiiBa", "datsir", "Beef", "FaZe Netan", "bloom"]
AM = ["CITIGAW", "coof", "ARM BLANKE", "CORPORALCATFISH", "Wicket", "Netanyahu's Cum Sock"]


def scoreboard(number: int = 1) -> dict[str, Any]:
    return json.loads((FIXTURES / f"scoreboard_real_{number}.json").read_text(encoding="utf-8"))


# --- Name matching ---------------------------------------------------------------------------


def _candidate(pid: int, name: str, confirmed: tuple[str, ...] = ()) -> ocr.Candidate:
    return ocr.Candidate(pid, name, frozenset(ocr.normalise(c) for c in confirmed), frozenset())


def test_close_names_map_as_a_whole() -> None:
    names = ["Nevermore", "AiiBa", "datsir"]
    cands = [_candidate(1, "datsir"), _candidate(2, "Nevermore."), _candidate(3, "aiiba")]
    assert [m.player_id for m in ocr.map_team(names, cands)] == [2, 3, 1]


def test_a_confirmed_in_game_name_maps_exactly() -> None:
    cands = [_candidate(1, "Discord Bob", ("Nevermore",)), _candidate(2, "Someone")]
    mapped = ocr.map_team(["Nevermore", "xyz"], cands)
    assert mapped[0].player_id == 1 and mapped[0].score == 1.0


def test_unclear_names_are_left_for_a_captain() -> None:
    cands = [_candidate(1, "alpha"), _candidate(2, "bravo")]
    assert all(m.player_id is None for m in ocr.map_team(["qqq", "zzz"], cands))


def test_parse_value_matches_the_reader() -> None:
    assert ocr.parse_value("38k") == 38_000
    assert ocr.parse_value("3.4k") == 3_400
    assert ocr.parse_value("12") == 12
    assert ocr.parse_value("1O") is None


# --- Through the flow ------------------------------------------------------------------------


@pytest.fixture
def season(league: Community) -> Season:
    Community.objects.filter(pk=league.pk).update(score_ocr="on")
    return start_season(league, {"placement_games": 0})


def _live_match(league: Community, players: list[Player]) -> Match:
    for player in players:
        queue.join(league.pk, player)
    match = to_in_progress(Match.objects.order_by("-id").first())
    hk = match.hidden_king_team
    other = "B" if hk == "A" else "A"
    for team, names in ((hk, HK), (other, AM)):
        rows = match.participants.filter(team=team).order_by("id").select_related("player")
        for row, name in zip(rows, names, strict=True):
            Player.objects.filter(pk=row.player_id).update(display_name=name)
    return fresh(match)


def _report(match: Match, winner: str) -> ResultSubmission:
    results.submit_report(match.pk, captain_of(match, winner).player, winner, image(1))
    return ResultSubmission.objects.filter(match=match).order_by("id").first()


def _player(name: str) -> Player:
    return Player.objects.get(display_name=name)


def test_a_clean_read_stores_stats_and_sets_the_awards(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    submission = _report(match, match.hidden_king_team)
    read = ocr.ingest(submission.pk, scoreboard(1))
    assert read.status in {ReadStatus.OK, ReadStatus.NEEDS_REVIEW}
    assert read.stats.filter(player__isnull=False).count() == 12
    nevermore = read.stats.get(player=_player("Nevermore"))
    assert (nevermore.kills, nevermore.deaths, nevermore.assists) == (12, 8, 13)
    assert nevermore.player_damage == 38_000 and nevermore.hero_name == "Mirage"
    assert ratings.current_awards(match) == {
        _player("Nevermore").pk: "mvp",
        _player("coof").pk: "key",
        _player("Netanyahu's Cum Sock").pk: "key",
    }
    submission.refresh_from_db()
    assert submission.deadlock_match_id == 131887902
    # The awards are in the rating once the other captain confirms.
    other = "B" if match.hidden_king_team == "A" else "A"
    results.confirm(match.pk, captain_of(match, other).player)
    mvp_event = RatingEvent.objects.get(match=match, player=_player("Nevermore"))
    assert mvp_event.award_adj is not None and mvp_event.award_adj > 0
    assert integrity.check_season(season) == []


def test_a_scoreboard_showing_the_other_winner_disputes_the_report(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    loser = "B" if match.hidden_king_team == "A" else "A"
    submission = _report(match, loser)
    ocr.ingest(submission.pk, scoreboard(1))
    assert fresh(match).status == Status.DISPUTED


def test_a_typed_match_id_is_never_overwritten(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    hk = match.hidden_king_team
    results.submit_report(match.pk, captain_of(match, hk).player, hk, image(1), 42)
    submission = ResultSubmission.objects.get(match=match)
    ocr.ingest(submission.pk, scoreboard(1))
    submission.refresh_from_db()
    assert submission.deadlock_match_id == 42


def test_a_read_after_the_result_corrects_the_ledger(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    hk = match.hidden_king_team
    other = "B" if hk == "A" else "A"
    submission = _report(match, hk)
    results.confirm(match.pk, captain_of(match, other).player)
    assert fresh(match).status == Status.COMPLETED
    ocr.ingest(submission.pk, scoreboard(1))
    assert RatingRun.objects.filter(match=match, kind=RunKind.CORRECTION).count() == 1
    assert OutboxEffect.objects.filter(kind="refresh_results", match=match).exists()
    assert integrity.check_season(season) == []


def test_shadow_mode_stores_stats_and_changes_nothing(
    season: Season, league: Community, players: list[Player]
) -> None:
    Community.objects.filter(pk=league.pk).update(score_ocr="shadow")
    match = _live_match(league, players)
    loser = "B" if match.hidden_king_team == "A" else "A"
    submission = _report(match, loser)
    read = ocr.ingest(submission.pk, scoreboard(1))
    assert read.stats.count() == 12
    assert ratings.current_awards(match) == {}
    assert fresh(match).status == Status.AWAITING_CONFIRMATION


def test_staff_awards_are_never_replaced_by_a_read(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    submission = _report(match, match.hidden_king_team)
    awards.record(match, {_player("bloom").pk: "mvp"}, AwardSource.STAFF, None)
    ocr.ingest(submission.pk, scoreboard(1))
    assert ratings.current_awards(match) == {_player("bloom").pk: "mvp"}


def test_unmatched_names_ask_a_captain_and_are_remembered(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    hk = match.hidden_king_team
    # Rename one team to names the reader can't place.
    for number, row in enumerate(match.participants.filter(team=hk).order_by("id")):
        Player.objects.filter(pk=row.player_id).update(display_name=f"Discord name {number}")
    submission = _report(match, hk)
    read = ocr.ingest(submission.pk, scoreboard(1))
    assert read.status == ReadStatus.NEEDS_MAPPING
    assert OutboxEffect.objects.filter(kind="scoreboard_notice", match=match).exists()
    assert ratings.current_awards(match) == {}  # The MVP is on the unmatched team.

    captain = captain_of(match, hk).player
    rows = ocr.unmapped_rows(read.pk)
    assert len(rows) == 6
    hk_players = list(match.participants.filter(team=hk).order_by("id"))
    left = None
    for row, participant in zip(rows, hk_players, strict=True):
        left = ocr.map_row(read.pk, row.row, participant.player_id, captain, is_staff=False)
    assert left == 0
    assert fresh(match).awards.exists()
    assert PlayerAlias.objects.filter(confirmed=True, name="Nevermore").exists()

    # Someone outside the match can't match names.
    with pytest.raises(Exception, match="captain or staff"):
        ocr.can_map(read.pk, _outsider(), False)


def _outsider() -> Player:
    from tests.conftest import make_player

    return make_player(77)


def test_a_player_can_only_be_matched_to_their_own_team(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    hk = match.hidden_king_team
    for number, row in enumerate(match.participants.filter(team=hk).order_by("id")):
        Player.objects.filter(pk=row.player_id).update(display_name=f"Discord name {number}")
    read = ocr.ingest(_report(match, hk).pk, scoreboard(1))
    row = ocr.unmapped_rows(read.pk)[0]
    other = "B" if hk == "A" else "A"
    wrong = match.participants.filter(team=other).first()
    with pytest.raises(InvalidAction):
        ocr.map_row(read.pk, row.row, wrong.player_id, captain_of(match, hk).player, is_staff=True)


def test_reports_queue_a_read_only_when_ocr_is_on(league: Community, players: list[Player]) -> None:
    match = _live_match(league, players)
    _report(match, match.hidden_king_team)
    assert not OutboxEffect.objects.filter(kind="read_scoreboard").exists()


def test_a_report_queues_a_read(season: Season, league: Community, players: list[Player]) -> None:
    match = _live_match(league, players)
    submission = _report(match, match.hidden_king_team)
    effect = OutboxEffect.objects.get(kind="read_scoreboard")
    assert effect.payload == {"submission_id": submission.pk}


def test_a_failed_read_is_recorded(
    season: Season, league: Community, players: list[Player]
) -> None:
    match = _live_match(league, players)
    submission = _report(match, match.hidden_king_team)
    read = ocr.record_failure(submission.pk, ReadStatus.REJECTED, "NotAScoreboard: no table")
    assert read.status == ReadStatus.REJECTED
    assert ocr.latest_read(match) is None
    assert ScoreboardRead.objects.count() == 1


@pytest.mark.skipif(not ocr.available(), reason="needs the ocr dependency group")
def test_the_real_engine_reads_the_fixture() -> None:
    data = (FIXTURES / "scoreboard_real_1.png").read_bytes()
    result = ocr.read_image(data)
    assert result["winner"] == "Hidden King"
    assert result["mvp"] == "Nevermore"


@pytest.mark.skipif(not ocr.available(), reason="needs the ocr dependency group")
def test_a_real_read_goes_all_the_way_into_the_rating(
    season: Season, league: Community, players: list[Player]
) -> None:
    """The real engine, on the real screenshot, through ingest, awards and the ledger."""
    match = _live_match(league, players)
    hk = match.hidden_king_team
    submission = _report(match, hk)
    read = ocr.ingest(
        submission.pk, ocr.read_image((FIXTURES / "scoreboard_real_1.png").read_bytes())
    )
    assert read.stats.filter(player__isnull=False).count() == 12
    assert ratings.current_awards(match)[_player("Nevermore").pk] == "mvp"
    other = "B" if hk == "A" else "A"
    results.confirm(match.pk, captain_of(match, other).player)
    assert RatingEvent.objects.get(match=match, player=_player("Nevermore")).award_adj > 0
    assert integrity.check_season(season) == []
