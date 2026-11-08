"""Races across real, separate database connections. The earlier prototype never tested this."""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from django.db import connections

from league.domain.errors import DomainError
from league.models import Community, DraftAction, Match, Player, QueueEntry
from league.services import community, integrity, lobby, queue, results
from league.services.common import captain_of
from tests.conftest import make_player
from tests.helpers import fresh, image, keep_captains, ready_all, to_in_progress

pytestmark = pytest.mark.django_db(transaction=True)


def race(*calls: Callable[[], object]) -> tuple[list[object], list[Exception]]:
    """Release all calls at once on their own threads, so each has its own connection."""
    barrier = threading.Barrier(len(calls))
    wins: list[object] = []
    losses: list[Exception] = []
    lock = threading.Lock()

    def run(call: Callable[[], object]) -> None:
        try:
            barrier.wait()
            result = call()
            with lock:
                wins.append(result)
        except DomainError as exc:
            with lock:
                losses.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return wins, losses


@pytest.fixture
def live_league(transactional_db: None) -> Community:
    from django.conf import settings

    from league.services import champions, modes

    presets = settings.BASE_DIR / "presets"
    champions.seed_from_file(presets / "champions.yaml")
    modes.publish(modes.load_yaml(presets / "threeban_1win_2026.yaml"))
    return community.ensure(guild_id=999, name="Test")


def full_lobby(league: Community) -> tuple[Match, list[Player]]:
    players = [make_player(n) for n in range(1, 13)]
    for player in players:
        queue.join(league.pk, player)
    return Match.objects.get(), players


def test_two_captains_racing_for_one_step_only_one_lands(live_league: Community) -> None:
    match, _players = full_lobby(live_league)
    match = keep_captains(ready_all(match))
    captain = captain_of(match, match.player_first_pick).player
    targets = list(match.participants.filter(team=""))[:2]
    from league.services import draft

    wins, losses = race(
        lambda: draft.pick_player(match.pk, captain, 0, targets[0].player_id, 111),
        lambda: draft.pick_player(match.pk, captain, 0, targets[1].player_id, 222),
    )
    assert len(wins) == 1
    assert len(losses) == 1
    assert DraftAction.objects.filter(match=match).count() == 1
    match = fresh(match)
    assert match.step_index == 1
    assert match.participants.exclude(team="").count() == 3


def test_simultaneous_joins_pop_exactly_one_lobby(live_league: Community) -> None:
    players = [make_player(n) for n in range(1, 25)]
    wins, losses = race(*[lambda p=p: queue.join(live_league.pk, p) for p in players])
    assert not losses
    assert len(wins) == 24
    assert Match.objects.count() == 2
    assert QueueEntry.objects.count() == 0
    for player in players:
        assert player.matches.filter(is_active=True).count() == 1


def test_double_join_race_leaves_one_entry(live_league: Community) -> None:
    player = make_player(1)
    wins, losses = race(*[lambda: queue.join(live_league.pk, player)] * 4)
    assert len(wins) == 1
    assert len(losses) == 3
    assert QueueEntry.objects.count() == 1


def test_click_racing_a_timeout_applies_once(live_league: Community) -> None:
    from django.utils import timezone

    from league.services import deadlines, draft

    match, _players = full_lobby(live_league)
    match = keep_captains(ready_all(match))
    Match.objects.filter(pk=match.pk).update(deadline_at=timezone.now())
    captain = captain_of(match, match.player_first_pick).player
    target = match.participants.filter(team="").first().player_id
    race(
        lambda: draft.pick_player(match.pk, captain, 0, target, 333),
        lambda: deadlines.fire_match_deadline(match.pk),
    )
    # Whichever landed first, each step has exactly one row and the index agrees with them.
    match = fresh(match)
    assert DraftAction.objects.filter(match=match).count() == match.step_index


def test_both_captains_confirming_at_once_rates_once(live_league: Community) -> None:
    match, _players = full_lobby(live_league)
    match = to_in_progress(match)
    captain_a = captain_of(match, "A").player
    captain_b = captain_of(match, "B").player
    wins, losses = race(
        lambda: results.submit_report(match.pk, captain_a, "A", image(1)),
        lambda: results.submit_report(match.pk, captain_b, "A", image(2)),
    )
    assert len(wins) == 2
    assert not losses
    match = fresh(match)
    assert match.status == "completed"
    assert match.results.count() == 1
    assert match.rating_events.count() == 12
    assert integrity.check_all() == []


def test_two_matches_finishing_with_the_ledger_consistent(live_league: Community) -> None:
    players = [make_player(n) for n in range(1, 25)]
    for player in players:
        queue.join(live_league.pk, player)
    first, second = list(Match.objects.order_by("id"))
    first = to_in_progress(first)
    second = to_in_progress(second)
    from tests.helpers import finish

    wins, losses = race(lambda: finish(first, "A", 1), lambda: finish(second, "B", 2))
    assert len(wins) == 2
    assert not losses
    assert integrity.check_all() == []


def test_ready_clicks_at_once_start_captains_once(live_league: Community) -> None:
    match, players = full_lobby(live_league)
    wins, losses = race(*[lambda p=p: lobby.mark_ready(match.pk, p) for p in players])
    assert len(wins) == 12
    assert not losses
    match = fresh(match)
    assert match.status == "captain_phase"
    assert match.participants.filter(is_captain=True).count() == 2
    assert match.transitions.filter(to_status="captain_phase").count() == 1
