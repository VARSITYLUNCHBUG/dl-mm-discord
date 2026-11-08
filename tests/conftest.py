from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from league.models import Community, Match, Player
from league.services import champions, common, community, modes, queue


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
        corrections,
        deadlines,
        draft,
        effects,
        lobby,
        penalties,
        results,
        subs,
    )
    from league.services import queue as queue_service
    from league.services import ratings as ratings_service

    for module in (
        corrections,
        deadlines,
        draft,
        effects,
        lobby,
        penalties,
        queue_service,
        ratings_service,
        results,
        subs,
    ):
        monkeypatch.setattr(module, "now", instance.now)
    return instance


@pytest.fixture
def league(db: None) -> Community:
    presets = settings.BASE_DIR / "presets"
    champions.seed_from_file(presets / "champions.yaml")
    for path in sorted(presets.glob("*.yaml")):
        if path.name not in {"champions.yaml", "champion_emojis.yaml"}:
            modes.publish(modes.load_yaml(path))
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
