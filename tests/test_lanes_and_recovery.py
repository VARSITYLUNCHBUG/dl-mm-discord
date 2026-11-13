"""Concurrent drafts stay independent, a match's players all go home when it ends, and a draft
command that fails part-way never leaves a hidden draft blocking its channel."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot.commands.drafts import captains_coinflip_command, startdraft_command
from league.models import Community, Match, OutboxEffect, PrioEntry, QuickDraft
from league.models.matches import ResourceKind
from league.models.outbox import EffectStatus
from league.models.quickdraft import QuickDraftStatus
from league.services import community as community_service
from league.services import effects, lobby, queue, quickdraft
from league.services.common import add_effect, now, render_queue
from tests.conftest import make_player
from tests.test_bot import (
    WAITING_ROOM,
    FakeGateway,
    _http_error,
    discord_ids,
    drain,
    resource,
    wired,  # noqa: F401 (a fixture)
)

CHANNEL = 4242
OTHER_CHANNEL = 4343
HK = 101
AM = 202
ELSEWHERE = 9999


# --- Outbox lanes ---------------------------------------------------------------------------


def _lanes() -> dict[object, list[int]]:
    return {
        effects.effect_lane(group[0]): [effect.pk for effect in group]
        for group in effects.due_by_match()
    }


@pytest.mark.django_db
def test_a_retrying_draft_does_not_hold_up_other_drafts_or_the_queue(league: Community) -> None:
    first = quickdraft.start(league.pk, CHANNEL, HK)
    second = quickdraft.start(league.pk, OTHER_CHANNEL, HK)
    OutboxEffect.objects.all().delete()
    add_effect(league, "render_quick_draft", {"draft_id": first.pk})
    failing = OutboxEffect.objects.get()
    # Discord refused it once; it's due again now.
    OutboxEffect.objects.filter(pk=failing.pk).update(attempts=1)
    add_effect(league, "delete_quick_draft", {"draft_id": first.pk, "channel_id": CHANNEL})
    later = OutboxEffect.objects.latest("id")
    add_effect(league, "render_quick_draft", {"draft_id": second.pk})
    render_queue(league)

    lanes = _lanes()
    # The first draft's later work waits behind its retry, as a match's would...
    assert lanes[f"quick_draft:{first.pk}"] == [failing.pk]
    assert all(later.pk not in pks for pks in lanes.values())
    # ...but the other draft and the queue panel have lanes of their own.
    assert f"quick_draft:{second.pk}" in lanes
    assert "render_queue" in lanes


@pytest.mark.django_db
def test_a_draft_retrying_later_does_not_block_anything(league: Community) -> None:
    first = quickdraft.start(league.pk, CHANNEL, HK)
    second = quickdraft.start(league.pk, OTHER_CHANNEL, HK)
    OutboxEffect.objects.all().delete()
    add_effect(league, "render_quick_draft", {"draft_id": first.pk})
    OutboxEffect.objects.update(attempts=3, run_after=now() + timedelta(minutes=5))
    add_effect(league, "render_quick_draft", {"draft_id": second.pk})
    assert list(_lanes()) == [f"quick_draft:{second.pk}"]


# --- Match end: everyone goes back to the Waiting Room --------------------------------------


def _gathered(wired: Community, gateway: FakeGateway) -> tuple[Match, list[int]]:  # noqa: F811
    players = [make_player(n) for n in range(1, 13)]
    ids = discord_ids(players)
    gateway.voice[WAITING_ROOM] = set(ids)
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    return Match.objects.get(), ids


@pytest.mark.django_db(transaction=True)
def test_match_end_sends_everyone_back_wherever_they_are(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    match, ids = _gathered(wired, gateway)
    lobby_voice = resource(match, ResourceKind.LOBBY_VOICE)
    # Players wander: one to another channel, one back to the Waiting Room, one out of voice.
    gateway.voice[lobby_voice] -= {ids[1], ids[2], ids[3]}
    gateway.voice[ELSEWHERE] = {ids[1]}
    gateway.voice[WAITING_ROOM] = {ids[2]}
    spectator = 555
    gateway.voice[lobby_voice].add(spectator)

    lobby.staff_void(match.pk, None, "test")
    drain(gateway)

    in_voice = set(ids) - {ids[3]}
    assert gateway.voice[WAITING_ROOM] == in_voice | {spectator}
    assert gateway.voice[ELSEWHERE] == set()
    assert lobby_voice in gateway.deleted
    # The one already sitting there got no voice event, so cleanup put them back on the list.
    assert PrioEntry.objects.filter(discord_user_id=ids[2], display_name="P3").exists()
    assert not OutboxEffect.objects.filter(status=EffectStatus.FAILED).exists()


@pytest.mark.django_db(transaction=True)
def test_a_player_already_in_a_new_match_is_left_alone(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    first, _ids = _gathered(wired, gateway)
    lobby.staff_void(first.pk, None, "test")
    # Before the first match's cleanup runs, one of its players is matched again.
    regular = make_player(1)
    gateway.voice = {ELSEWHERE: {regular.discord_user_id}}
    for player in [regular, *(make_player(n) for n in range(20, 31))]:
        queue.join(wired.pk, player)
    second = Match.objects.exclude(pk=first.pk).get()
    assert second.participants.filter(player=regular, is_active=True).exists()

    assert regular.discord_user_id not in effects.players_to_send_back(first.pk)
    drain(gateway)
    assert regular.discord_user_id in gateway.voice[ELSEWHERE]


@pytest.mark.django_db(transaction=True)
def test_one_unmovable_player_does_not_stop_cleanup(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    match, ids = _gathered(wired, gateway)
    lobby_voice = resource(match, ResourceKind.LOBBY_VOICE)
    gateway.fail_next[f"move_member:{ids[0]}"] = _http_error(403, 50013)

    lobby.staff_void(match.pk, None, "test")
    drain(gateway)

    assert set(ids[1:]) <= gateway.voice[WAITING_ROOM]
    assert lobby_voice in gateway.deleted
    assert not OutboxEffect.objects.filter(status=EffectStatus.FAILED).exists()


# --- /startdraft: acknowledge first, never leave a hidden draft -------------------------------


class FakeResponse:
    def __init__(self, events: list[Any]) -> None:
        self.events = events
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.events.append("defer")
        self.done = True

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        self.events.append("send")
        self.done = True


class FakeFollowup:
    def __init__(self, events: list[Any]) -> None:
        self.events = events

    async def send(self, text: str, *, ephemeral: bool = False, **kwargs: Any) -> None:
        self.events.append(("followup", text, ephemeral))


class FakeInteraction:
    """Enough of a slash command interaction for the draft commands."""

    id = 1
    channel_id = CHANNEL
    client = SimpleNamespace(wake_effects=lambda: None)

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.response = FakeResponse(self.events)
        self.followup = FakeFollowup(self.events)
        self.guild = SimpleNamespace(id=999, name="Test")
        self.user = SimpleNamespace(id=HK, display_name="Hidden")
        self.fail_post: Exception | None = None
        self.message: SimpleNamespace | None = None

    async def edit_original_response(self, *, view: discord.ui.LayoutView) -> SimpleNamespace:
        if self.fail_post is not None:
            raise self.fail_post
        self.events.append("posted")
        self.message = SimpleNamespace(id=777_000 + len(self.events))
        return self.message

    async def delete_original_response(self) -> None:
        self.events.append("deleted")
        self.message = None

    def errors(self) -> list[str]:
        return [event[1] for event in self.events if isinstance(event, tuple) and event[2]]


def _run(command: Any, interaction: FakeInteraction, *args: Any) -> None:
    asyncio.run(command.callback(interaction, *args))


def _live() -> QuickDraft | None:
    return quickdraft.live_in_channel(CHANNEL)


@pytest.fixture
def db_calls(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeInteraction], None]:
    """Records the command's first database call in its interaction's event list."""

    def watch(interaction: FakeInteraction) -> None:
        real = community_service.ensure

        def ensure(*args: Any, **kwargs: Any) -> Community:
            interaction.events.append("db")
            return real(*args, **kwargs)

        monkeypatch.setattr(community_service, "ensure", ensure)

    return watch


@pytest.mark.django_db(transaction=True)
def test_startdraft_acknowledges_before_any_database_work(
    wired: Community,  # noqa: F811
    db_calls: Callable[[FakeInteraction], None],
) -> None:
    interaction = FakeInteraction()
    db_calls(interaction)
    _run(startdraft_command, interaction)
    assert interaction.events[:2] == ["defer", "db"]
    assert interaction.events[-1] == "posted"
    draft = _live()
    assert draft is not None and interaction.message is not None
    assert draft.message_id == interaction.message.id
    assert draft.status == QuickDraftStatus.CAPTAINS


@pytest.mark.django_db(transaction=True)
def test_captains_coinflip_posts_the_same_way(wired: Community) -> None:  # noqa: F811
    interaction = FakeInteraction()
    hk = SimpleNamespace(id=HK, display_name="Hidden")
    am = SimpleNamespace(id=AM, display_name="Arch")
    _run(captains_coinflip_command, interaction, hk, am)
    assert interaction.events[0] == "defer"
    draft = _live()
    assert draft is not None and draft.status == QuickDraftStatus.COINFLIP
    assert interaction.message is not None and draft.message_id == interaction.message.id


@pytest.mark.django_db(transaction=True)
def test_a_draft_that_cannot_be_posted_is_closed_so_a_retry_works(
    wired: Community,  # noqa: F811
) -> None:
    interaction = FakeInteraction()
    interaction.fail_post = _http_error(500, 0)
    _run(startdraft_command, interaction)
    assert _live() is None
    assert QuickDraft.objects.get().status == QuickDraftStatus.STOPPED
    assert "deleted" in interaction.events  # No "thinking..." left behind.
    assert interaction.errors() == ["Something went wrong on our side. Please try again."]

    retry = FakeInteraction()
    _run(startdraft_command, retry)
    draft = _live()
    assert draft is not None and retry.message is not None
    assert draft.message_id == retry.message.id


@pytest.mark.django_db(transaction=True)
def test_a_draft_whose_message_cannot_be_saved_is_taken_down(
    wired: Community,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object) -> bool:
        raise RuntimeError("database went away")

    monkeypatch.setattr(quickdraft, "attach_message", broken)
    interaction = FakeInteraction()
    _run(startdraft_command, interaction)
    assert interaction.message is None  # The posted draft was deleted again.
    assert _live() is None
    assert interaction.errors() == ["The draft couldn't be set up. Please run the command again."]


@pytest.mark.django_db(transaction=True)
def test_if_closing_fails_too_the_tick_frees_the_channel(
    wired: Community,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object) -> None:
        raise RuntimeError("database went away")

    with monkeypatch.context() as patch:
        patch.setattr(quickdraft, "attach_message", broken)
        patch.setattr(quickdraft, "message_gone", broken)
        interaction = FakeInteraction()
        _run(startdraft_command, interaction)
    assert interaction.message is None
    stuck = _live()
    assert stuck is not None and stuck.message_id is None

    assert quickdraft.close_unposted(now()) == 0  # Too soon: it may still be being posted.
    later = now() + timedelta(seconds=quickdraft.UNPOSTED_SECONDS + 1)
    assert quickdraft.close_unposted(later) == 1
    assert _live() is None


@pytest.mark.django_db(transaction=True)
def test_a_refused_startdraft_leaves_only_the_error(wired: Community) -> None:  # noqa: F811
    existing = quickdraft.start(wired.pk, CHANNEL, AM)
    quickdraft.attach_message(existing.pk, 123)
    interaction = FakeInteraction()
    _run(startdraft_command, interaction)
    assert "posted" not in interaction.events
    assert "deleted" in interaction.events
    assert interaction.errors() == ["A draft is already in progress in this channel!"]
    assert QuickDraft.objects.count() == 1


@pytest.mark.django_db
def test_a_message_for_a_closed_draft_is_not_attached(league: Community) -> None:
    draft = quickdraft.start(league.pk, CHANNEL, HK)
    quickdraft.message_gone(draft.pk)
    assert not quickdraft.attach_message(draft.pk, 123)
    assert QuickDraft.objects.get(pk=draft.pk).message_id is None
