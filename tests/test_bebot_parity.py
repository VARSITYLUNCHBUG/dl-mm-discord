"""Bebot's features beyond the match flow: /startdraft, the coinflips, /randomdraft, the voice
priority list, named pings, the error channel and Bebot's draft look."""

from __future__ import annotations

import logging

import discord
import pytest

from bot.error_log import BURST_LIMIT, DiscordChannelLogHandler, format_record
from bot.renderers import bebot
from bot.renderers.prio import prio_text, render_prio
from bot.renderers.quickdraft import (
    hero_grid,
    render_coinflip,
    render_quick_draft,
    render_random_draft,
)
from bot.routing import registered_actions
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.models import Champion, Community, Match, OutboxEffect, PrioEntry, QuickDraft
from league.models.quickdraft import QuickDraftStatus, Side
from league.services import lobby, pings, prio, queue, quickdraft
from league.services.common import now
from tests.conftest import Clock, make_player
from tests.test_bot import (
    FakeGateway,
    all_buttons,
    all_custom_ids,
    assert_within_limits,
    drain,
    wired,  # noqa: F401 (a fixture)
)

HK = 101
AM = 202
STRANGER = 303
CHANNEL = 4242


def _labels(view: discord.ui.LayoutView) -> list[str]:
    return [button.get("label", "") for button in all_buttons(view.to_components())]


def _render(draft_id: int) -> discord.ui.LayoutView:
    view = render_quick_draft(quickdraft.state(draft_id))
    assert_within_limits(view)
    for custom_id in all_custom_ids(view.to_components()):
        assert custom_id.split(":")[2] in registered_actions(), custom_id
    return view


def _grid(draft_id: int) -> list[discord.ui.LayoutView]:
    views = hero_grid(quickdraft.state(draft_id))
    for view in views:
        assert_within_limits(view)
        for custom_id in all_custom_ids(view.to_components()):
            assert custom_id.split(":")[2] in registered_actions(), custom_id
    return views


def _grid_buttons(draft_id: int) -> list[dict[str, object]]:
    return [b for view in _grid(draft_id) for b in all_buttons(view.to_components())]


def _drafting(league: Community) -> QuickDraft:
    draft = quickdraft.start(league.pk, CHANNEL, HK)
    quickdraft.claim_captain(draft.pk, "hk", HK, "Hidden", allow_both=False)
    quickdraft.claim_captain(draft.pk, "am", AM, "Arch", allow_both=False)
    return QuickDraft.objects.get(pk=draft.pk)


def _acting(draft: QuickDraft) -> int:
    state = quickdraft.state(draft.pk)
    user_id, _name = state.captain(state.acting_side)
    assert user_id is not None
    return user_id


# --- Bebot's look ---------------------------------------------------------------------------


BEBOT_ORDER = [
    ("ban", "hk"), ("ban", "am"), ("pick", "hk"), ("pick", "am"), ("pick", "am"),
    ("pick", "hk"), ("pick", "hk"), ("pick", "am"), ("ban", "am"), ("ban", "hk"),
    ("pick", "am"), ("pick", "hk"), ("pick", "hk"), ("pick", "am"), ("pick", "am"),
    ("pick", "hk"),
]  # fmt: skip


def test_timeline_matches_bebots_turn_strip() -> None:
    # Checked against Bebot's own generate_turn_str for every turn of this order.
    assert bebot.timeline(BEBOT_ORDER, 0) == (
        "Bans\n[🔶]🔷            🔷🔶\n"
        f"{bebot.TIMELINE_RULE}\n"
        "Picks\n      🔶🔷🔷🔶🔶🔷   🔷🔶🔶🔷🔷🔶"
    )
    current = bebot.timeline(BEBOT_ORDER, 9)
    assert "🔷[🔶]" in current.splitlines()[1]


def test_timeline_without_bans_is_one_line() -> None:
    assert bebot.timeline([("pick", "hk"), ("pick", "am")], 1) == "🔶 [🔷]"


def test_hero_emoji_text_prefers_custom_then_unicode() -> None:
    assert bebot.hero_emoji_text("Mo & Krill", 99, "🦀") == "<:mo___krill:99>"
    assert bebot.hero_emoji_text("Apollo", None, "🗡️") == "🗡️"
    assert bebot.hero_line("", "Haze", random_choice=True) == "Haze (random choice)"
    assert bebot.hero_line("🐐", "Billy", auto=True) == "🐐 Billy (randomed)"


@pytest.mark.django_db
def test_bebots_unicode_hero_emojis_are_loaded(league: Community) -> None:
    assert Champion.objects.get(name="Apollo").unicode_emoji == "🗡️"
    assert Champion.objects.get(name="Nurse Harrow").unicode_emoji == "💉"
    assert Champion.objects.get(name="Abrams").unicode_emoji == ""


# --- /startdraft ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_startdraft_posts_bebots_two_captain_buttons(league: Community) -> None:
    draft = quickdraft.start(league.pk, CHANNEL, HK)
    labels = _labels(_render(draft.pk))
    assert labels == ["🔶 Become The Hidden King Captain", "🔷 Become The Archmother Captain"]
    with pytest.raises(InvalidAction, match="already in progress"):
        quickdraft.start(league.pk, CHANNEL, STRANGER)


@pytest.mark.django_db
def test_one_person_cannot_captain_both_sides(league: Community) -> None:
    draft = quickdraft.start(league.pk, CHANNEL, HK)
    assert quickdraft.claim_captain(draft.pk, "hk", HK, "Hidden", allow_both=False) == (
        "The Hidden King"
    )
    with pytest.raises(InvalidAction, match="already the captain"):
        quickdraft.claim_captain(draft.pk, "hk", HK, "Hidden", allow_both=False)
    with pytest.raises(InvalidAction, match="both teams"):
        quickdraft.claim_captain(draft.pk, "am", HK, "Hidden", allow_both=False)
    with pytest.raises(InvalidAction, match="already has a captain"):
        quickdraft.claim_captain(draft.pk, "hk", STRANGER, "Someone", allow_both=False)
    # In DEBUG, as in Bebot, one tester can take both sides.
    quickdraft.claim_captain(draft.pk, "am", HK, "Hidden", allow_both=True)
    assert QuickDraft.objects.get(pk=draft.pk).status == QuickDraftStatus.DRAFTING


@pytest.mark.django_db
def test_draft_message_is_bebots_summary_with_the_match_grid_below(league: Community) -> None:
    draft = _drafting(league)
    view = _render(draft.pk)
    text = str(view.to_components())
    assert "🏆 Draft #" in text
    assert f"🔶 <@{HK}> ban a hero! ⌛ <t:" in text
    assert "🔷 Arch" in text
    assert "🚫 **Bans**\\nNone" in text
    assert "[🔶]" in text
    assert _labels(view) == []  # The heroes are in messages of their own, as in the match flow.
    container = view.to_components()[0]
    assert container["accent_color"] == bebot.HK_COLOUR
    # Every hero at once, no pages: two balanced messages, Random last.
    grid = _grid(draft.pk)
    assert len(grid) == 2
    buttons = _grid_buttons(draft.pk)
    heroes = Champion.objects.filter(is_enabled=True).count()
    assert len(buttons) == heroes + 1
    assert buttons[-1]["label"] == "Random"
    names = [b["label"] for b in buttons[:-1]]
    assert names == sorted(names)


@pytest.mark.django_db
def test_only_the_captain_on_the_clock_can_choose(league: Community) -> None:
    draft = _drafting(league)
    hero = quickdraft.state(draft.pk).available[0].id
    with pytest.raises(NotAllowed, match="not your turn to ban"):
        quickdraft.choose_hero(draft.pk, AM, 0, hero)
    quickdraft.choose_hero(draft.pk, HK, 0, hero)
    with pytest.raises(StaleAction):
        quickdraft.choose_hero(draft.pk, AM, 0, hero)  # A late click on the old step.
    with pytest.raises(InvalidAction, match="already been banned"):
        quickdraft.choose_hero(draft.pk, AM, 1, hero)
    state = quickdraft.state(draft.pk)
    assert hero not in {option.id for option in state.available}  # Bebot drops taken heroes.
    assert state.acting_side == Side.ARCHMOTHER


@pytest.mark.django_db
def test_a_taken_hero_stays_in_place_greyed_out(league: Community) -> None:
    draft = _drafting(league)
    before = [b["label"] for b in _grid_buttons(draft.pk)]
    hero = quickdraft.state(draft.pk).heroes[7]
    quickdraft.choose_hero(draft.pk, HK, 0, hero.id)
    after = _grid_buttons(draft.pk)
    assert [b["label"] for b in after] == before  # Nothing shifts.
    assert after[7]["disabled"] is True
    assert not any(b.get("disabled") for i, b in enumerate(after) if i != 7)


@pytest.mark.django_db
def test_a_timed_out_turn_is_randomed(league: Community, clock: Clock) -> None:
    draft = _drafting(league)
    clock.advance(41)
    assert quickdraft.tick(clock.now()) == 1
    text = str(_render(draft.pk).to_components())
    assert "(randomed)" in text
    assert QuickDraft.objects.get(pk=draft.pk).step_index == 1


@pytest.mark.django_db
def test_a_finished_draft_ends_with_glhf_and_a_gif(league: Community) -> None:
    draft = _drafting(league)
    for _ in range(16):
        current = QuickDraft.objects.get(pk=draft.pk)
        quickdraft.choose_random(draft.pk, _acting(current), current.step_index)
    draft.refresh_from_db()
    assert draft.status == QuickDraftStatus.COMPLETED
    view = _render(draft.pk)
    text = str(view.to_components())
    assert "# GLHF" in text
    assert "tenor.com" in text
    assert text.count("(random choice)") == 16
    assert _grid(draft.pk) == []  # The grid is gone.


@pytest.mark.django_db
def test_stopdraft_is_for_captains_starters_and_staff(league: Community) -> None:
    draft = _drafting(league)
    quickdraft.attach_message(draft.pk, 777)
    with pytest.raises(NotAllowed):
        quickdraft.stop(CHANNEL, STRANGER, is_staff=False)
    quickdraft.stop(CHANNEL, AM, is_staff=False)
    assert QuickDraft.objects.get(pk=draft.pk).status == QuickDraftStatus.STOPPED
    assert OutboxEffect.objects.filter(kind="delete_quick_draft").exists()
    with pytest.raises(InvalidAction, match="No draft"):
        quickdraft.stop(CHANNEL, AM, is_staff=True)
    quickdraft.start(league.pk, CHANNEL, STRANGER)  # The channel is free again.


# --- /captains_coinflip ---------------------------------------------------------------------


def _coinflip(league: Community) -> QuickDraft:
    return quickdraft.start_coinflip(league.pk, CHANNEL, HK, (HK, "Hidden"), (AM, "Arch"))


@pytest.mark.django_db
def test_coinflip_winner_chooses_then_the_loser(league: Community) -> None:
    draft = _coinflip(league)
    winner, loser = draft.coinflip_winner_id, draft.coinflip_loser_id
    assert {winner, loser} == {HK, AM}
    assert _labels(_render(draft.pk)) == [
        "First Pick",
        "Last Pick",
        "🔶 Hidden King",
        "🔷 Archmother",
    ]
    with pytest.raises(NotAllowed, match="coinflip winner"):
        quickdraft.coinflip_choose(draft.pk, loser, quickdraft.FIRST_PICK)
    quickdraft.coinflip_choose(draft.pk, winner, quickdraft.LAST_PICK)
    assert _labels(_render(draft.pk)) == ["🔶 Hidden King", "🔷 Archmother"]
    with pytest.raises(InvalidAction):
        quickdraft.coinflip_choose(draft.pk, loser, quickdraft.FIRST_PICK)
    quickdraft.coinflip_choose(draft.pk, loser, quickdraft.HIDDEN_KING)
    draft.refresh_from_db()
    # The loser took The Hidden King; the winner chose last pick, so the loser goes first.
    assert draft.status == QuickDraftStatus.READY
    assert draft.hk_captain_id == loser
    assert draft.am_captain_id == winner
    assert draft.first_side == Side.HIDDEN_KING
    assert _labels(_render(draft.pk)) == ["Start Draft"]
    with pytest.raises(NotAllowed):
        quickdraft.start_after_coinflip(draft.pk, STRANGER)
    quickdraft.start_after_coinflip(draft.pk, winner)
    assert QuickDraft.objects.get(pk=draft.pk).status == QuickDraftStatus.DRAFTING


@pytest.mark.django_db
def test_coinflip_can_give_the_archmother_first_pick(league: Community) -> None:
    draft = _coinflip(league)
    winner = draft.coinflip_winner_id
    loser = draft.coinflip_loser_id
    assert winner is not None
    assert loser is not None
    quickdraft.coinflip_choose(draft.pk, winner, quickdraft.ARCHMOTHER)
    quickdraft.coinflip_choose(draft.pk, loser, quickdraft.LAST_PICK)
    quickdraft.start_after_coinflip(draft.pk, winner)
    state = quickdraft.state(draft.pk)
    assert state.first_side == Side.ARCHMOTHER
    assert state.acting_side == Side.ARCHMOTHER
    assert state.am_captain_id == winner
    container = _render(draft.pk).to_components()[0]
    assert container["accent_color"] == bebot.AM_COLOUR


@pytest.mark.django_db
def test_coinflip_between_the_same_player_is_refused(league: Community) -> None:
    with pytest.raises(InvalidAction, match="same player"):
        quickdraft.start_coinflip(league.pk, CHANNEL, HK, (HK, "A"), (HK, "A"))


def test_plain_coinflip_says_who_takes_the_hidden_king() -> None:
    text = str(render_coinflip((HK, "Hidden"), (AM, "Arch")).to_components())
    assert "**Hidden** wins the flip!" in text
    assert f"<@{HK}> should select The Hidden King (first pick)" in text
    assert f"<@{AM}> should select The Archmother" in text


@pytest.mark.django_db
def test_randomdraft_uses_every_step_once(league: Community) -> None:
    result = quickdraft.random_draft(league)
    heroes = [h.id for side in ("hk", "am") for h in result.bans[side] + result.picks[side]]
    assert len(heroes) == 16 == len(set(heroes))
    assert len(result.bans["hk"]) == len(result.bans["am"]) == 2
    assert len(result.picks["hk"]) == len(result.picks["am"]) == 6
    text = str(render_random_draft(result).to_components())
    assert "🏆 Draft 🏆 (Random)" in text


# --- Effects --------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_turns_edit_the_draft_message_in_place(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    draft = quickdraft.start(wired.pk, CHANNEL, HK)
    message_id = 900_001
    gateway.messages[CHANNEL] = [message_id]
    quickdraft.attach_message(draft.pk, message_id)
    quickdraft.claim_captain(draft.pk, "hk", HK, "Hidden", allow_both=False)
    quickdraft.claim_captain(draft.pk, "am", AM, "Arch", allow_both=False)
    drain(gateway)
    assert message_id in gateway.edited
    assert "ban a hero!" in str(gateway.views[message_id].to_components())
    # The grid sits directly below it, in two messages of its own.
    grid = QuickDraft.objects.get(pk=draft.pk).grid_message_ids
    assert len(grid) == 2
    assert gateway.messages[CHANNEL] == [message_id, *grid]


def _posted_draft(wired: Community, gateway: FakeGateway) -> QuickDraft:  # noqa: F811
    draft = quickdraft.start(wired.pk, CHANNEL, HK)
    message_id = 900_001
    gateway.messages[CHANNEL] = [message_id]
    quickdraft.attach_message(draft.pk, message_id)
    quickdraft.claim_captain(draft.pk, "hk", HK, "Hidden", allow_both=False)
    quickdraft.claim_captain(draft.pk, "am", AM, "Arch", allow_both=False)
    drain(gateway)
    return QuickDraft.objects.get(pk=draft.pk)


@pytest.mark.django_db(transaction=True)
def test_each_turn_posts_the_grid_afresh(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    draft = _posted_draft(wired, gateway)
    old_grid = draft.grid_message_ids
    quickdraft.choose_random(draft.pk, HK, 0)
    drain(gateway)
    draft.refresh_from_db()
    # Never edited, so no "(edited)" tag splits the grid; the old one is gone.
    assert gateway.messages[CHANNEL] == [draft.message_id, *draft.grid_message_ids]
    assert not set(old_grid) & set(gateway.messages[CHANNEL])
    assert not gateway.edited & set(draft.grid_message_ids)


@pytest.mark.django_db(transaction=True)
def test_chat_below_moves_the_draft_down(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    draft = _posted_draft(wired, gateway)
    old_message = draft.message_id
    chat = gateway._id()
    gateway.messages[CHANNEL].append(chat)
    quickdraft.choose_random(draft.pk, HK, 0)
    drain(gateway)
    draft.refresh_from_db()
    assert draft.message_id != old_message
    assert gateway.messages[CHANNEL] == [chat, draft.message_id, *draft.grid_message_ids]


@pytest.mark.django_db(transaction=True)
def test_finishing_or_stopping_removes_the_grid(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    draft = _posted_draft(wired, gateway)
    for _ in range(16):
        current = QuickDraft.objects.get(pk=draft.pk)
        quickdraft.choose_random(draft.pk, _acting(current), current.step_index)
    drain(gateway)
    draft.refresh_from_db()
    assert draft.grid_message_ids == []
    assert gateway.messages[CHANNEL] == [draft.message_id]  # Just the GLHF summary.

    second = _posted_draft(wired, FakeGateway())
    gateway2 = FakeGateway()
    gateway2.messages[CHANNEL] = [second.message_id, *second.grid_message_ids]
    quickdraft.stop(CHANNEL, HK, is_staff=False)
    drain(gateway2)
    assert gateway2.messages[CHANNEL] == []


@pytest.mark.django_db(transaction=True)
def test_a_deleted_draft_channel_frees_the_draft(wired: Community) -> None:  # noqa: F811
    gateway = FakeGateway()
    draft = quickdraft.start(wired.pk, CHANNEL, HK)
    quickdraft.attach_message(draft.pk, 900_002)
    gateway.deleted.append(CHANNEL)
    quickdraft.claim_captain(draft.pk, "hk", HK, "Hidden", allow_both=False)
    drain(gateway)
    assert QuickDraft.objects.get(pk=draft.pk).status == QuickDraftStatus.STOPPED


# --- Pings ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_ping_has_a_cooldown(league: Community, clock: Clock) -> None:
    pings.add(league.pk, CHANNEL, " PUGS ", "<@&55>", 15, None)
    ping = pings.claim(CHANNEL, None, set())
    assert ping.name == "pugs"
    with pytest.raises(InvalidAction, match="on cooldown \\(15m left\\)"):
        pings.claim(CHANNEL, "pugs", set())
    clock.advance(15 * 60 + 1)
    pings.claim(CHANNEL, "pugs", set())


@pytest.mark.django_db
def test_pings_need_a_name_when_a_channel_has_several(league: Community) -> None:
    pings.add(league.pk, CHANNEL, "pugs", "<@&55>", 15, None)
    pings.add(league.pk, CHANNEL, "rank", "<@&66>", 5, 77)
    with pytest.raises(InvalidAction, match="Pick which ping"):
        pings.claim(CHANNEL, None, set())
    with pytest.raises(InvalidAction, match="No ping named `nope`"):
        pings.claim(CHANNEL, "nope", set())
    with pytest.raises(NotAllowed, match="<@&77>"):
        pings.claim(CHANNEL, "rank", {55})
    assert pings.claim(CHANNEL, "rank", {77}).mention == "<@&66>"
    with pytest.raises(InvalidAction, match="at least 1 minute"):
        pings.add(league.pk, CHANNEL, "x", "y", 0, None)
    assert pings.remove(CHANNEL, "RANK")
    assert not pings.remove(CHANNEL, "rank")


@pytest.mark.django_db
def test_no_pings_in_a_channel(league: Community) -> None:
    with pytest.raises(InvalidAction, match="/pingchannel"):
        pings.claim(CHANNEL, None, set())


@pytest.mark.django_db
def test_error_channel_is_stored_per_server(league: Community) -> None:
    pings.set_error_channel(league.pk, 999)
    assert pings.error_channels() == [999]
    pings.set_error_channel(league.pk, None)
    assert pings.error_channels() == []


def test_error_log_posts_bebots_format() -> None:
    handler = DiscordChannelLogHandler()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("bot.test").makeRecord(
            "bot.test", logging.ERROR, __file__, 1, "It broke", (), __import__("sys").exc_info()
        )
    text = format_record(handler, record)
    assert text.startswith("**[ERROR]** `bot.test` — It broke\n```\nTraceback")
    assert "ValueError: boom" in text


def test_error_log_caps_a_burst() -> None:
    handler = DiscordChannelLogHandler()
    allowed = [handler._allowed() for _ in range(BURST_LIMIT + 5)]
    assert allowed.count(True) == BURST_LIMIT


# --- Priority list --------------------------------------------------------------------------


WAITING = 8001
ELSEWHERE = 8003


@pytest.fixture
def room(league: Community) -> Community:
    league.waiting_room_channel_id = WAITING
    league.prio_channel_id = 9100
    league.save()
    return league


def _move(league: Community, user_id: int, before: int | None, after: int | None) -> bool:
    return prio.on_voice_state(league.pk, user_id, f"U{user_id}", before, after)


def _lines(league: Community) -> list[tuple[str, str]]:
    return [(line.status, line.name) for line in prio.prio_view(league.pk).lines]


@pytest.mark.django_db
def test_waiting_room_puts_you_on_the_list(room: Community, clock: Clock) -> None:
    _move(room, 1, None, WAITING)
    clock.advance(120)
    _move(room, 2, None, WAITING)
    assert _lines(room) == [("🟢", "U1"), ("🟢", "U2")]
    assert "`1.` 🟢 **U1** (2m)" in prio_text(prio.prio_view(room.pk))
    assert OutboxEffect.objects.filter(kind="render_prio").count() == 1  # Coalesced.


@pytest.mark.django_db
def test_leaving_starts_a_grace_period(room: Community, clock: Clock) -> None:
    _move(room, 1, None, WAITING)
    _move(room, 2, None, WAITING)
    _move(room, 1, WAITING, None)
    assert _lines(room) == [("⏳", "U1"), ("🟢", "U2")]
    assert "Grace: 4m" in prio_text(prio.prio_view(room.pk))
    clock.advance(60)
    _move(room, 1, None, WAITING)  # Back in time: first place kept.
    assert _lines(room) == [("🟢", "U1"), ("🟢", "U2")]
    _move(room, 1, WAITING, ELSEWHERE)
    clock.advance(301)
    prio.tick(clock.now())
    assert _lines(room) == [("🟢", "U2")]


@pytest.mark.django_db
def test_a_match_takes_you_off_until_it_ends_then_you_go_on_the_back(
    room: Community, clock: Clock
) -> None:
    players = [make_player(number) for number in range(1, 13)]
    for player in players:
        _move(room, player.discord_user_id, None, WAITING)
    clock.advance(60)
    _move(room, 1, None, WAITING)  # Someone who isn't playing.
    assert len(_lines(room)) == 13

    for player in players:
        queue.join(room.pk, player)
    match = Match.objects.get()
    assert _lines(room) == [("🟢", "U1")]  # The twelve are off as soon as the match exists.

    # While it's live, where they go in voice doesn't matter, and the queue refuses them.
    first = players[0]
    assert not _move(room, first.discord_user_id, ELSEWHERE, WAITING)
    _move(room, players[1].discord_user_id, WAITING, ELSEWHERE)
    assert _lines(room) == [("🟢", "U1")]
    with pytest.raises(InvalidAction, match="already in match"):
        queue.join(room.pk, first)

    lobby.staff_void(match.pk, None, "test")
    # Back in the Waiting Room after the match: on the back of the list, behind U1.
    _move(room, first.discord_user_id, ELSEWHERE, WAITING)
    assert _lines(room) == [("🟢", "U1"), ("🟢", f"U{first.discord_user_id}")]


@pytest.mark.django_db
def test_list_leaves_out_anyone_in_a_live_match(room: Community) -> None:
    """A safety net: even an entry that slipped through isn't shown while they play."""
    players = [make_player(number) for number in range(1, 13)]
    for player in players:
        queue.join(room.pk, player)
    PrioEntry.objects.create(
        community=room,
        discord_user_id=players[0].discord_user_id,
        display_name="P1",
        queued_at=now(),
    )
    assert _lines(room) == []


@pytest.mark.django_db
def test_removeprio_and_reconcile(room: Community) -> None:
    _move(room, 1, None, WAITING)
    assert prio.remove(room.pk, 1)
    assert not prio.remove(room.pk, 1)
    _move(room, 2, None, WAITING)
    # After a restart: 3 sat in the Waiting Room unseen, and 2 had gone.
    prio.reconcile(room.pk, {3: "U3"})
    assert _lines(room) == [("⏳", "U2"), ("🟢", "U3")]


@pytest.mark.django_db
def test_reconcile_names_players_it_only_knows_by_id(room: Community) -> None:
    player = make_player(7)
    prio.reconcile(room.pk, {player.discord_user_id: ""})
    assert _lines(room) == [("🟢", "P7")]


@pytest.mark.django_db
def test_other_channels_are_ignored(room: Community) -> None:
    assert not _move(room, 5, None, ELSEWHERE)
    assert not _move(room, 5, ELSEWHERE, None)
    assert not PrioEntry.objects.exists()


@pytest.mark.django_db
def test_no_waiting_room_means_no_list(league: Community) -> None:
    assert not prio.on_voice_state(league.pk, 5, "U5", None, WAITING)
    assert not PrioEntry.objects.exists()


def test_empty_priority_list() -> None:
    text = str(render_prio(prio.PrioView([])).to_components())
    assert "Queue is empty!" in text
    assert "⏳ Grace period" in text
    assert "Recently played" not in text
