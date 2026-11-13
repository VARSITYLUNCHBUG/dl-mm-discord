from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from league.models import Community, DraftModeVersion, Match, Player
from league.services import champions, common, community, modes, queue

PRESETS = settings.BASE_DIR / "presets"
NOT_MODES = frozenset({"champions.yaml", "champion_emojis.yaml"})


def publish_presets(*, captain_rule: bool = False) -> None:
    """Seed the champions and publish every preset draft mode, as `load_presets` does.

    Without `captain_rule`, anyone can captain: most tests are about something else, and twelve
    brand-new players would otherwise always skip the vote. The `captain_rule` fixture publishes
    the presets as shipped.
    """
    champions.seed_from_file(PRESETS / "champions.yaml")
    for path in sorted(PRESETS.glob("*.yaml")):
        if path.name not in NOT_MODES:
            body = modes.load_yaml(path)
            if not captain_rule:
                captains = dict(body.get("captains") or {})  # type: ignore[call-overload]
                captains.update(min_games=0, max_strikes=None)
                body["captains"] = captains
            modes.publish(body)


def activate(league: Community, slug: str) -> Community:
    """Switch new lobbies to another preset. Every preset runs the same flow."""
    version = DraftModeVersion.objects.filter(slug=slug).order_by("-version").first()
    assert version is not None, slug
    modes.activate(league.pk, version.pk)
    league.refresh_from_db()
    return league


class Clock:
    """Lets tests move time without sleeping."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> datetime:
        return timezone.now() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    instance = Clock()
    monkeypatch.setattr(common, "now", instance.now)
    # Services import `now` by name, so each one needs patching too.
    from league.services import (
        api_results,
        awards,
        corrections,
        deadlines,
        draft,
        early_leave,
        effects,
        lobby,
        ocr,
        penalties,
        results,
        standing,
        steam,
        subs,
    )
    from league.services import queue as queue_service
    from league.services import ratings as ratings_service

    for module in (
        api_results,
        awards,
        corrections,
        deadlines,
        draft,
        early_leave,
        effects,
        lobby,
        ocr,
        penalties,
        queue_service,
        ratings_service,
        results,
        standing,
        steam,
        subs,
    ):
        monkeypatch.setattr(module, "now", instance.now)
    return instance


@pytest.fixture
def league(db: None) -> Community:
    publish_presets()
    return community.ensure(guild_id=999, name="Test")


def make_player(number: int) -> Player:
    # Well above the fake-player range, like a real Discord ID.
    return common.get_or_create_player(10_000_000 + number, f"P{number}")


@pytest.fixture
def players(league: Community) -> list[Player]:
    return [make_player(number) for number in range(1, 13)]


@pytest.fixture
def popped(league: Community, players: list[Player]) -> Match:
    """A lobby in its ready check, created by twelve players joining."""
    for player in players:
        queue.join(league.pk, player)
    return Match.objects.get()


@pytest.fixture
def captain_rule(league: Community) -> Community:
    """New lobbies use the presets' real captain rule: 5 completed matches, under 3 strikes."""
    publish_presets(captain_rule=True)
    return activate(league, league.active_mode.slug)


@pytest.fixture
def voting(popped: Match) -> Match:
    """Everyone is ready and the captain vote is open."""
    from tests.helpers import ready_all

    return ready_all(popped)
