"""The Discord layer: routing, rendering, and the whole flow through effects on a fake gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from django.db import connections

from bot.client import DraftBot
from bot.db import db
from bot.effects.gateway import ChannelGone
from bot.effects.worker import EffectWorker
from bot.handlers import draft as _draft  # noqa: F401
from bot.handlers import lobby as _lobby  # noqa: F401
from bot.handlers import queue as _queue  # noqa: F401
from bot.handlers import results as _results  # noqa: F401
from bot.handlers.results import parse_deadlock_match_id
from bot.images import detect_image
from bot.renderers.board import render_board, render_pickers, render_ready_dm
from bot.renderers.queue import render_queue
from bot.renderers.results import render_results, team_table
from bot.routing import (
    BUTTON_TEMPLATE,
    SELECT_TEMPLATE,
    RouteButton,
    button_id,
    registered_actions,
    select_id,
)
from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import Status
from league.domain.errors import InvalidAction
from league.models import Community, Match, OutboxEffect, Player
from league.models.matches import ResourceKind
from league.models.outbox import EffectStatus
from league.services import boards, community, dev, effects, integrity, lobby, queue
from league.services import draft as draft_service
from league.services import results as results_service
from league.services.boards import ResultRow
from league.services.common import add_effect, captain_of, get_or_create_player, mode_of
from league.services.common import render_queue as request_queue
from tests.conftest import make_player, publish_presets
from tests.helpers import (
    acting_captain,
    claim_sides,
    finish,
    fresh,
    ready_all,
    run_champion_draft,
    run_player_draft,
    to_champion_draft,
    to_in_progress,
    vote_captains,
)

# Discord's limits for a Components V2 message.
MAX_COMPONENTS = 40
MAX_TEXT = 4000


def count_components(components: list[dict[str, Any]]) -> int:
    total = 0
    for component in components:
        total += 1
        total += count_components(component.get("components", []))
        accessory = component.get("accessory")
        if accessory:
            total += 1
    return total


def total_text(components: list[dict[str, Any]]) -> int:
    total = 0
    for component in components:
        if component.get("type") == 10:
            total += len(component.get("content", ""))
        total += total_text(component.get("components", []))
    return total


def assert_within_limits(view: discord.ui.LayoutView) -> None:
    payload = view.to_components()
    assert count_components(payload) <= MAX_COMPONENTS
    assert total_text(payload) <= MAX_TEXT


def all_custom_ids(components: list[dict[str, Any]]) -> list[str]:
    found = []
    for component in components:
        if "custom_id" in component:
            found.append(component["custom_id"])
        found.extend(all_custom_ids(component.get("components", [])))
    return found


def all_buttons(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for component in components:
        if component.get("type") == 2:
            found.append(component)
        found.extend(all_buttons(component.get("components", [])))
    return found


def all_selects(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for component in components:
        if component.get("type") == 3:
            found.append(component)
        found.extend(all_selects(component.get("components", [])))
    return found


# --- Routing -------------------------------------------------------------------------------


def test_button_and_select_ids_match_their_templates() -> None:
    import re

    assert re.fullmatch(BUTTON_TEMPLATE, button_id("ready", 42))
    assert re.fullmatch(BUTTON_TEMPLATE, button_id("claim_side", 42, "hk"))
    assert re.fullmatch(SELECT_TEMPLATE, select_id("pick_champion", 42, "12.1"))
    assert not re.fullmatch(BUTTON_TEMPLATE, select_id("pick_champion", 42, "12.1"))
    assert BUTTON_TEMPLATE != SELECT_TEMPLATE


def test_custom_ids_fit_discords_limit() -> None:
    assert len(select_id("captain_vote", 10**12, "12345.12")) <= 100


@pytest.mark.django_db
def test_every_rendered_action_has_a_handler(league: Community, players: list[Player]) -> None:
    for player in players:
        queue.join(league.pk, player)
    match = Match.objects.get()
    states = [boards.board_state(match.pk)]
    match = ready_all(match)
    states.append(boards.board_state(match.pk))
    match = run_player_draft(vote_captains(match))
    states.append(boards.board_state(match.pk))
    match = claim_sides(match)
    states.append(boards.board_state(match.pk))
    actions = registered_actions()
    for state in states:
        for custom_id in all_custom_ids(render_board(state).to_components()):
            assert custom_id.split(":")[2] in actions, custom_id


# --- Rendering -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_phase_renders_within_discord_limits(popped: Match) -> None:
    from league.services import draft, results

    seen = set()

    def check(match: Match) -> None:
        state = boards.board_state(match.pk)
        seen.add(state.status)
        views = [render_board(state), *render_pickers(state)]
        for view in views:
            assert_within_limits(view)
            text = str(view.to_components())
            assert "Team A" not in text
            assert "Team B" not in text
            ids = all_custom_ids(view.to_components())
            assert len(ids) == len(set(ids)), "custom_ids must be unique in a message"
            for custom_id in ids:
                assert custom_id.split(":")[2] in registered_actions(), custom_id

    check(popped)
    match = ready_all(popped)
    check(match)
    match = vote_captains(match)
    check(match)
    match = run_player_draft(match)
    check(match)
    match = claim_sides(match)
    check(match)
    match = run_champion_draft(match)
    check(match)
    draft.submit_lobby_code(match.pk, match.lobby_code_host, "ABC123")
    match = fresh(match)
    check(match)
    from tests.helpers import image

    results.submit_report(match.pk, captain_of(match, "A").player, "A", image())
    match = fresh(match)
    check(match)
    results.dispute(match.pk, captain_of(match, "B").player, "No")
    match = fresh(match)
    check(match)
    results.resolve_dispute(match.pk, "A", None)
    check(fresh(match))
    assert seen == {
        Status.READY_CHECK,
        Status.CAPTAIN_PHASE,
        Status.PLAYER_DRAFT,
        Status.SIDE_SELECT,
        Status.CHAMPION_DRAFT,
        Status.LOBBY_CODE,
        Status.IN_PROGRESS,
        Status.AWAITING_CONFIRMATION,
        Status.DISPUTED,
        Status.COMPLETED,
    }


def draft_buttons(view: discord.ui.LayoutView) -> list[dict[str, Any]]:
    """The draft's own buttons, leaving out the Request a sub and Random buttons."""
    skip = (":sub_", ":random_champion:")
    return [
        b
        for b in all_buttons(view.to_components())
        if not any(part in b.get("custom_id", "") for part in skip)
    ]


def grid_buttons(state: boards.BoardState) -> list[dict[str, Any]]:
    return [button for view in render_pickers(state) for button in draft_buttons(view)]


@pytest.mark.django_db
def test_champion_grid_has_messages_of_its_own_below_the_board(popped: Match) -> None:
    match = to_champion_draft(popped)
    state = boards.board_state(match.pk)
    assert len(state.champions) == 44
    # The board is the box alone: the draft so far and Request a sub, no champions.
    components = render_board(state).to_components()
    assert [component["type"] for component in components] == [17]
    assert "choose_champion" not in str(components)
    assert "sub_open" in str(all_custom_ids(components))
    # The grid is two messages of nothing but rows, so they stack into one grid.
    first, second = render_pickers(state)
    for view in (first, second):
        assert_within_limits(view)
        assert {component["type"] for component in view.to_components()} == {1}
    assert len(draft_buttons(first)) == 25
    assert len(draft_buttons(second)) == 19
    assert [b["label"] for b in all_buttons(second.to_components())][-1] == "Random"
    labels = [button["label"] for button in grid_buttons(state)]
    assert labels == sorted(labels)  # Alphabetical across both messages.


@pytest.mark.django_db
def test_a_taken_champion_stays_in_place_greyed_out(popped: Match) -> None:
    match = to_champion_draft(popped)
    before = grid_buttons(boards.board_state(match.pk))
    assert {button["style"] for button in before} == {discord.ButtonStyle.danger.value}  # Bans.

    step = build_plan(mode_of(match))[match.step_index]
    captain = captain_of(match, team_for_role(step.role, match.champion_first_pick)).player
    target = boards.board_state(match.pk).champions[3]
    draft_service.choose_champion(match.pk, captain, match.step_index, target.id)

    state = boards.board_state(match.pk)
    after = grid_buttons(state)
    assert [button["label"] for button in after] == [button["label"] for button in before]
    assert after[3]["disabled"] is True
    assert after[3]["style"] == discord.ButtonStyle.secondary.value
    assert not after[4].get("disabled")
    assert len(state.available_champions) == 43


@pytest.mark.django_db
def test_champion_buttons_turn_green_on_a_pick(popped: Match) -> None:
    match = to_champion_draft(popped)
    plan = build_plan(mode_of(match))
    while plan[match.step_index].action != "pick":
        step = plan[match.step_index]
        captain = captain_of(match, team_for_role(step.role, match.champion_first_pick)).player
        champion = draft_service.available_champions(match)[0]
        draft_service.choose_champion(match.pk, captain, match.step_index, champion)
        match = fresh(match)
    styles = {
        b["style"] for b in grid_buttons(boards.board_state(match.pk)) if not b.get("disabled")
    }
    assert styles == {discord.ButtonStyle.success.value}


@pytest.mark.django_db
def test_champion_board_says_how_far_along_the_draft_is(popped: Match) -> None:
    match = to_champion_draft(popped)
    state = boards.board_state(match.pk)
    payload = str(render_board(state).to_components())
    assert f"**{state.acting_captain_name}** is banning: ban 1 of 4." in payload


@pytest.mark.django_db
def test_player_draft_shows_everyone_as_buttons_and_never_reflows(popped: Match) -> None:
    match = vote_captains(ready_all(popped))
    before = draft_buttons(render_board(boards.board_state(match.pk)))
    assert len(before) == 10
    assert not all_selects(render_board(boards.board_state(match.pk)).to_components())

    step = build_plan(mode_of(match))[match.step_index]
    captain = captain_of(match, team_for_role(step.role, match.player_first_pick)).player
    target = match.participants.filter(is_active=True, team="").order_by("id").first()
    draft_service.pick_player(match.pk, captain, match.step_index, target.player_id)

    after = draft_buttons(render_board(boards.board_state(match.pk)))
    assert [button["label"] for button in after] == [button["label"] for button in before]
    assert sum(1 for button in after if button.get("disabled")) == 1


@pytest.mark.django_db
def test_player_draft_says_which_pick_of_a_double_turn_it_is(popped: Match) -> None:
    match = vote_captains(ready_all(popped))
    step = build_plan(mode_of(match))[match.step_index]
    captain = captain_of(match, team_for_role(step.role, match.player_first_pick)).player
    target = match.participants.filter(is_active=True, team="").order_by("id").first()
    draft_service.pick_player(match.pk, captain, match.step_index, target.player_id)
    state = boards.board_state(match.pk)
    payload = str(render_board(state).to_components())
    assert f"**{state.acting_captain_name}** is picking (1 of 2)." in payload


def test_oversized_roster_falls_back_to_dropdowns() -> None:
    from league.services.boards import BoardState, ChampionOption

    state = BoardState(
        match_id=1,
        status=Status.CHAMPION_DRAFT,
        mode_name="Test",
        is_candidate_format=False,
        deadline_at=None,
        step_index=10,
        players=[],
        champions=[ChampionOption(i, f"Champion {i}") for i in range(120)],
    )
    assert_within_limits(render_board(state))
    (picker,) = render_pickers(state)
    assert_within_limits(picker)
    assert sum(len(select["options"]) for select in all_selects(picker.to_components())) == 120
    assert "random_champion" in str(picker.to_components())


def test_largest_grid_that_fits_stays_within_limits() -> None:
    from league.services.boards import BoardState, ChampionOption

    state = BoardState(
        match_id=1,
        status=Status.CHAMPION_DRAFT,
        mode_name="Test",
        is_candidate_format=False,
        deadline_at=None,
        step_index=10,
        players=[],
        champions=[ChampionOption(i, f"Champion {i}") for i in range(55)],
    )
    pickers = render_pickers(state)
    assert len(pickers) == 2
    assert_within_limits(render_board(state))
    for picker in pickers:
        assert_within_limits(picker)
    assert len(grid_buttons(state)) == 55


def _state(status: str, **extra: Any) -> boards.BoardState:
    extra.setdefault("deadline_at", None)
    return boards.BoardState(
        match_id=1,
        status=status,
        mode_name="3-ban",
        is_candidate_format=True,
        step_index=0,
        players=[],
        **extra,
    )


def test_format_footer_only_shows_on_the_ready_check() -> None:
    assert "Format:" in str(render_board(_state(Status.READY_CHECK)).to_components())
    assert "Format:" not in str(render_board(_state(Status.IN_PROGRESS)).to_components())


def test_settings_checklist_is_only_for_the_host() -> None:
    assert "Lobby settings" in str(render_board(_state(Status.LOBBY_CODE)).to_components())
    assert "Lobby settings" not in str(
        render_board(_state(Status.IN_PROGRESS, lobby_code="ABC")).to_components()
    )


def test_game_on_board_reports_with_a_button_per_team() -> None:
    captains = {"A": "MDMT", "B": "Bo"}
    view = render_board(_state(Status.IN_PROGRESS, lobby_code="ABC", captains=captains))
    buttons = all_buttons(view.to_components())
    assert [button["label"] for button in buttons] == ["MDMT's Team won", "Bo's Team won"]
    assert [button["custom_id"].split(":")[-1] for button in buttons] == ["A", "B"]


@pytest.mark.parametrize(
    ("status", "action", "label"),
    [
        (Status.READY_CHECK, "", "Ready check closes"),
        (Status.PLAYER_DRAFT, "player_pick", "Auto-pick"),
        (Status.CAPTAIN_PHASE, "", "Vote closes"),
        (Status.SIDE_SELECT, "", "Coin flip"),
        (Status.CHAMPION_DRAFT, "ban", "Auto-pick"),
        (Status.CHAMPION_DRAFT, "pick", "Auto-pick"),
        (Status.AWAITING_CONFIRMATION, "", "Confirms automatically"),
    ],
)
def test_timers_say_what_happens_when_they_run_out(status: str, action: str, label: str) -> None:
    from datetime import UTC, datetime

    from bot.renderers.board import countdown
    from league.services.boards import BoardState

    state = BoardState(
        match_id=1,
        status=status,
        mode_name="Test",
        is_candidate_format=False,
        deadline_at=datetime(2030, 1, 1, tzinfo=UTC),
        step_index=0,
        players=[],
        step_action=action,
    )
    assert label in countdown(state)
    assert "<t:" in countdown(state)


def test_fake_player_is_not_rendered_as_a_broken_mention() -> None:
    from bot.renderers.board import player_mention

    assert player_mention(2, "Fake 2") == "@Fake 2"
    assert player_mention(123456789, "MDMT") == "<@123456789>"


def test_candidate_format_note_is_kept_off_the_players_board() -> None:
    from league.services.boards import BoardState

    state = BoardState(
        match_id=1,
        status=Status.READY_CHECK,
        mode_name="3-ban",
        is_candidate_format=True,
        deadline_at=None,
        step_index=0,
        players=[],
    )
    payload = str(render_board(state).to_components())
    assert "Format: 3-ban" in payload
    assert "unconfirmed" not in payload


def test_game_on_timer_is_a_footer_not_the_headline() -> None:
    from datetime import UTC, datetime

    from bot.renderers.board import countdown

    state = _state(
        Status.IN_PROGRESS, lobby_code="ABC", deadline_at=datetime(2030, 1, 1, tzinfo=UTC)
    )
    assert countdown(state) == ""
    assert "or staff will step in" in str(render_board(state).to_components())


def test_draft_turn_names_who_goes_next() -> None:
    state = _state(
        Status.CHAMPION_DRAFT,
        acting_captain_name="Bo",
        step_action="ban",
        turn_position=1,
        turn_total=6,
        next_captain_name="MDMT",
        next_action="pick",
    )
    assert "Next: **MDMT** picks." in str(render_board(state).to_components())


def test_queue_links_the_waiting_room_and_says_how_many_are_needed() -> None:
    view = render_queue(
        boards.QueueView(
            size=11,
            capacity=12,
            players=[f"P{n}" for n in range(11)],
            mode_name="3-ban",
            live_matches=1,
            waiting_room_channel_id=7001,
        )
    )
    payload = str(view.to_components())
    assert "<#7001>" in payload
    assert "1 more to start" in payload
    assert "1 match in progress" in payload


@pytest.mark.django_db
def test_results_show_each_teams_change(popped: Match) -> None:
    match = finish(to_in_progress(popped), "A")
    model = boards.results_view(match.pk)
    view = render_results(model, attachment_name="match-1.png")
    assert_within_limits(view)
    payload = str(view.to_components())
    assert "attachment://match-1.png" in payload
    captain = boards.captain_names(match)["A"]
    assert f"{captain}'s Team won" in payload
    assert "> **+16** | __1016__ | " in team_table(model.team_a)
    assert "> **-16** | __984__ | " in team_table(model.team_b)
    for word in ("Team A", "Team B", "Hidden King", "Archmother", "K 32"):
        assert word not in payload


def _row(name: str, user_id: int = 123456789, before: int = 1500, after: int = 1512) -> ResultRow:
    return ResultRow(
        player_id=1,
        discord_user_id=user_id,
        name=name,
        before=before,
        after=after,
        is_captain=False,
    )


def test_results_lines_are_quotes_with_mentions_and_bold_deltas() -> None:
    table = team_table([_row("MDMT"), _row("Bo", 987654321, 980, 968)])
    assert table == (
        "> **+12** | __1512__ | <@123456789>\n> **-12** | \u2007__968__ | <@987654321>"
    )
    assert "```" not in table


def test_fake_players_show_a_name_rather_than_a_broken_mention() -> None:
    assert team_table([_row("Fake 3", 3, 1000, 988)]) == "> **-12** | __988__ | @Fake 3"


def test_single_digit_change_is_padded_so_pipes_line_up() -> None:
    from bot.renderers.results import render_correction
    from league.services.boards import CorrectionLine, CorrectionView

    lines = [CorrectionLine(123456789, "A", 12, 1512), CorrectionLine(987654321, "B", 9, 1009)]
    view = render_correction(CorrectionView(1, "voided", "Bug", lines, 0))
    content = view.to_components()[0]["components"][0]["content"]
    assert "> \u2007**+9** | __1009__ | <@987654321>" in content


def test_correction_post_uses_the_same_lines() -> None:
    from bot.renderers.results import render_correction
    from league.services.boards import CorrectionLine, CorrectionView

    view = render_correction(
        CorrectionView(1, "voided", "Bug", [CorrectionLine(123456789, "MDMT", -12, 1488)], 0)
    )
    assert "> **-12** | __1488__ | <@123456789>" in str(view.to_components())


@pytest.mark.django_db
def test_queue_renders(league: Community, players: list[Player]) -> None:
    queue.join(league.pk, players[0])
    view = render_queue(boards.queue_view(league))
    assert_within_limits(view)
    assert "1/12" in str(view.to_components())


def test_ready_dm_has_invite_link_and_ready_button() -> None:
    view = render_ready_dm(5, "https://discord.gg/abc", heading="Your match is ready")
    payload = str(view.to_components())
    assert "https://discord.gg/abc" in payload
    assert button_id("ready", 5) in payload
    assert_within_limits(view)


# --- Screenshots ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x89PNG\r\n\x1a\nrest", "png"),
        (b"\xff\xd8\xff\xe0rest", "jpg"),
        (b"RIFF\x00\x00\x00\x00WEBPrest", "webp"),
        (b"GIF89a", None),
        (b"<svg></svg>", None),
        (b"MZ\x90\x00", None),
        (b"", None),
    ],
)
def test_detect_image_checks_bytes(data: bytes, expected: str | None) -> None:
    assert detect_image(data) == expected


def test_deadlock_match_id_parsing() -> None:
    assert parse_deadlock_match_id("") is None
    assert parse_deadlock_match_id(" 122069248 ") == 122069248
    with pytest.raises(InvalidAction):
        parse_deadlock_match_id("abc")


# --- Whole flow on a fake gateway ----------------------------------------------------------


@dataclass
class FakeGateway:
    """Records calls. Voice state is a dict of channel to users, so moves can be asserted."""

    voice: dict[int, set[int]] = field(default_factory=dict)
    channels: dict[int, str] = field(default_factory=dict)
    deleted: list[int] = field(default_factory=list)
    dms: list[tuple[int, discord.ui.LayoutView]] = field(default_factory=list)
    texts: list[tuple[int, str, list[int]]] = field(default_factory=list)
    views: dict[int, Any] = field(default_factory=dict)
    edited: set[int] = field(default_factory=set)  # Messages edited in place.
    posted_files: list[tuple[int, tuple[str, bytes] | None]] = field(default_factory=list)
    statuses: dict[int, str] = field(default_factory=dict)
    granted: list[tuple[int, int]] = field(default_factory=list)
    revoked: list[tuple[int, int]] = field(default_factory=list)
    members_seen: dict[int, list[int]] = field(default_factory=dict)
    closed_dms: set[int] = field(default_factory=set)
    next_id: int = 500_000
    fail_next: dict[str, Exception] = field(default_factory=dict)
    messages: dict[int, list[int]] = field(default_factory=dict)
    text_by_id: dict[int, str] = field(default_factory=dict)

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _maybe_fail(self, method: str) -> None:
        if method in self.fail_next:
            raise self.fail_next.pop(method)

    async def create_text_channel(
        self, guild_id, category_id, name, member_ids, staff_role_id
    ) -> int:
        self._maybe_fail("create_text_channel")
        channel_id = self._id()
        self.channels[channel_id] = name
        self.members_seen[channel_id] = list(member_ids)
        return channel_id

    async def create_voice_channel(
        self, guild_id, category_id, name, member_ids, staff_role_id
    ) -> int:
        channel_id = self._id()
        self.channels[channel_id] = name
        self.voice[channel_id] = set()
        self.members_seen[channel_id] = list(member_ids)
        return channel_id

    def voice_member_ids(self, guild_id, channel_id) -> set[int]:
        return set(self.voice.get(channel_id, set()))

    async def move_member(self, guild_id, user_id, channel_id) -> bool:
        current = [c for c, users in self.voice.items() if user_id in users]
        if not current:
            return False  # Discord's 40032.
        for channel in current:
            self.voice[channel].discard(user_id)
        self.voice.setdefault(channel_id, set()).add(user_id)
        return True

    async def create_invite(self, guild_id, channel_id, max_age) -> str | None:
        return f"https://discord.gg/invite-{channel_id}"

    async def send_dm(self, user_id, view) -> bool:
        if user_id in self.closed_dms:
            return False  # Discord's 50007.
        self.dms.append((user_id, view))
        return True

    async def send_view(self, channel_id, view, *, file=None) -> int:
        if channel_id in self.deleted:
            raise ChannelGone(str(channel_id))
        self._maybe_fail("send_view")
        message_id = self._id()
        self.views[message_id] = view
        self.posted_files.append((channel_id, file))
        self.messages.setdefault(channel_id, []).append(message_id)
        return message_id

    async def edit_view(self, channel_id, message_id, view) -> None:
        if channel_id in self.deleted:
            raise ChannelGone(str(channel_id))
        if message_id not in self.messages.get(channel_id, []):
            raise ChannelGone(str(message_id))
        self.views[message_id] = view
        self.edited.add(message_id)

    async def send_text(
        self, channel_id, text, *, user_ids: Iterable[int] = (), role_id=None
    ) -> int:
        if channel_id in self.deleted:
            raise ChannelGone(str(channel_id))
        self.texts.append((channel_id, text, list(user_ids)))
        message_id = self._id()
        self.text_by_id[message_id] = text
        self.messages.setdefault(channel_id, []).append(message_id)
        return message_id

    def chat(self, channel_id: int) -> int:
        """A player's message: it lands in the channel and the bot hears about it."""
        message_id = self._id()
        self.messages.setdefault(channel_id, []).append(message_id)
        effects.note_chat(channel_id)
        return message_id

    async def latest_message_id(self, channel_id) -> int | None:
        if channel_id in self.deleted:
            raise ChannelGone(str(channel_id))
        rows = self.messages.get(channel_id)
        return rows[-1] if rows else None

    async def delete_message(self, channel_id, message_id) -> None:
        if channel_id in self.deleted:
            raise ChannelGone(str(channel_id))
        if message_id in self.messages.get(channel_id, []):
            self.messages[channel_id].remove(message_id)

    async def delete_channel(self, channel_id) -> None:
        self.deleted.append(channel_id)
        self.voice.pop(channel_id, None)

    async def grant_member(self, channel_id, user_id) -> None:
        self.granted.append((channel_id, user_id))

    async def revoke_member(self, channel_id, user_id) -> None:
        self.revoked.append((channel_id, user_id))

    async def set_voice_status(self, channel_id, text) -> None:
        self.statuses[channel_id] = text

    async def fetch_attachment(self, channel_id, message_id, attachment_id):
        return ("shot.png", b"\x89PNG\r\n\x1a\ndata")


WAITING_ROOM = 7001
QUEUE_CHANNEL = 7002
RESULTS_CHANNEL = 7003
ARCHIVE_CHANNEL = 7004


@pytest.fixture
def wired(transactional_db: None) -> Iterator[Community]:
    publish_presets()
    row = community.ensure(guild_id=999, name="Test")
    community.save_wiring(
        row.pk,
        waiting_room_channel_id=WAITING_ROOM,
        queue_channel_id=QUEUE_CHANNEL,
        results_channel_id=RESULTS_CHANNEL,
        archive_channel_id=ARCHIVE_CHANNEL,
    )
    row.refresh_from_db()
    yield row
    # The worker's DB thread holds its own connection, which would block dropping the test DB.
    asyncio.run(db(connections.close_all))


def drain(gateway: FakeGateway) -> None:
    asyncio.run(EffectWorker(gateway).drain())


def resource(match: Match, kind: str) -> int:
    channel = effects.resource_id(match.pk, kind)
    assert channel is not None
    return channel


def discord_ids(players: list[Player]) -> list[int]:
    return [player.discord_user_id for player in players]


@pytest.mark.django_db(transaction=True)
def test_gather_moves_waiting_room_and_dms_everyone_else(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    ids = discord_ids(players)
    gateway = FakeGateway()
    # Four wait in the Waiting Room, one in some other voice channel, the rest not in voice.
    gateway.voice[WAITING_ROOM] = set(ids[:4])
    gateway.voice[9999] = {ids[4]}
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)

    match = Match.objects.get()
    lobby_voice = resource(match, ResourceKind.LOBBY_VOICE)
    assert gateway.voice[lobby_voice] == set(ids[:4])
    assert gateway.voice[WAITING_ROOM] == set()
    assert gateway.voice[9999] == {ids[4]}  # Left alone: they get an invite instead.
    assert {user for user, _view in gateway.dms} == set(ids[4:])
    payload = str(gateway.dms[0][1].to_components())
    assert f"invite-{lobby_voice}" in payload
    assert button_id("ready", match.pk) in payload


@pytest.mark.django_db(transaction=True)
def test_closed_dms_fall_back_to_a_lobby_mention(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    ids = discord_ids(players)
    gateway = FakeGateway(closed_dms={ids[0]})
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    text_id = resource(match, ResourceKind.TEXT)
    mentions = [
        row for row in gateway.texts if row[0] == text_id and ids[0] in row[2] and "DM" in row[1]
    ]
    assert mentions, "A player who can't be DMed must still be told"


@pytest.mark.django_db(transaction=True)
def test_lobby_channels_grant_real_members_only(wired: Community) -> None:
    fakes = dev.fake_players(11)
    real = make_player(1)
    gateway = FakeGateway()
    for player in [real, *fakes]:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    assert gateway.members_seen[resource(match, ResourceKind.TEXT)] == [real.discord_user_id]
    # Fake players ready themselves up, so only the real player is outstanding.
    waiting = match.participants.filter(is_active=True, ready_at__isnull=True)
    assert [row.player_id for row in waiting] == [real.pk]


@pytest.mark.django_db(transaction=True)
def test_joining_waiting_room_after_pop_targets_the_lobby(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    lobby_voice = resource(match, ResourceKind.LOBBY_VOICE)
    user_id = players[0].discord_user_id
    assert effects.voice_target(user_id, WAITING_ROOM) == (999, lobby_voice)
    assert effects.voice_target(user_id, lobby_voice) is None
    assert effects.voice_target(user_id, 12345) is None  # Their own business.
    assert effects.voice_target(make_player(50).discord_user_id, WAITING_ROOM) is None


@pytest.mark.django_db(transaction=True)
def test_full_match_through_effects(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    ids = discord_ids(players)
    gateway = FakeGateway()
    gateway.voice[WAITING_ROOM] = set(ids)
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    lobby_voice = resource(match, ResourceKind.LOBBY_VOICE)
    text_id = resource(match, ResourceKind.TEXT)
    assert gateway.voice[lobby_voice] == set(ids)
    assert resource(match, ResourceKind.BOARD_MESSAGE)
    assert not gateway.dms

    match = to_champion_draft(match)
    drain(gateway)
    team_a = resource(match, ResourceKind.TEAM_A_VOICE)
    team_b = resource(match, ResourceKind.TEAM_B_VOICE)
    a_ids = set(
        match.participants.filter(team="A").values_list("player__discord_user_id", flat=True)
    )
    b_ids = set(
        match.participants.filter(team="B").values_list("player__discord_user_id", flat=True)
    )
    assert gateway.voice[team_a] == a_ids
    assert gateway.voice[team_b] == b_ids
    assert lobby_voice in gateway.deleted
    assert effects.resource_id(match.pk, ResourceKind.LOBBY_VOICE) is None

    match = to_in_progress(match)
    drain(gateway)
    assert any("Lobby code: `ABC123`" in row[1] for row in gateway.texts)
    assert gateway.statuses[team_a] == "Lobby code: ABC123"

    match = finish(match, "A")
    drain(gateway)
    posts = [row for row in gateway.posted_files if row[0] == RESULTS_CHANNEL]
    assert len(posts) == 1
    assert posts[0][1][1].startswith(b"\x89PNG")
    assert resource(match, ResourceKind.RESULTS_MESSAGE)
    # Everyone heads back to the Waiting Room and team channels go.
    assert gateway.voice[WAITING_ROOM] == set(ids)
    assert team_a in gateway.deleted
    assert team_b in gateway.deleted
    # The text channel waits out its grace period.
    assert text_id not in gateway.deleted
    delayed = OutboxEffect.objects.get(kind="delete_text_channel", match=match)
    OutboxEffect.objects.filter(pk=delayed.pk).update(run_after=delayed.created_at)
    drain(gateway)
    assert text_id in gateway.deleted
    assert integrity.check_all() == []
    assert not OutboxEffect.objects.filter(status=EffectStatus.FAILED).exists()


@pytest.mark.django_db(transaction=True)
def test_player_not_in_lobby_voice_gets_team_invite(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    gateway.dms.clear()
    to_champion_draft(Match.objects.get())
    drain(gateway)
    assert len(gateway.dms) == 12
    assert "Join team voice" in str(gateway.dms[0][1].to_components())


def _http_error(status: int, code: int) -> discord.HTTPException:
    class Response:
        def __init__(self) -> None:
            self.status = status
            self.reason = "error"

    return discord.HTTPException(Response(), {"code": code, "message": "error"})  # type: ignore[arg-type]


@pytest.mark.django_db(transaction=True)
def test_refused_lobby_creation_voids_and_requeues(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway(fail_next={"create_text_channel": _http_error(403, 50013)})
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get(status=Status.VOIDED)
    effect = OutboxEffect.objects.get(kind="create_lobby", match=match)
    assert effect.status == EffectStatus.FAILED
    # Everyone went back in line, which is itself a full queue, so the next tick re-pops them.
    assert not match.participants.filter(is_active=True).exists()
    from league.models import QueueEntry

    assert QueueEntry.objects.count() == 12


@pytest.mark.django_db(transaction=True)
def test_server_errors_retry_and_block_later_effects_in_order(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway(fail_next={"create_text_channel": _http_error(503, 0)})
    for player in players:
        queue.join(wired.pk, player)
    asyncio.run(EffectWorker(gateway).run_once())
    match = Match.objects.get()
    effect = OutboxEffect.objects.get(kind="create_lobby", match=match)
    assert effect.status == EffectStatus.PENDING
    assert effect.attempts == 1
    gather = OutboxEffect.objects.filter(kind="gather_player", match=match)
    assert not gather.exclude(status=EffectStatus.PENDING).exists()  # Held behind the retry.

    OutboxEffect.objects.filter(pk=effect.pk).update(run_after=effect.created_at)
    drain(gateway)
    effect.refresh_from_db()
    assert effect.status == EffectStatus.DONE
    assert not gather.filter(status=EffectStatus.PENDING).exists()


@pytest.mark.django_db(transaction=True)
def test_repeated_create_lobby_does_not_duplicate_channels(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    channels_before = len(gateway.channels)
    effect = OutboxEffect.objects.get(kind="create_lobby", match=match)
    OutboxEffect.objects.filter(pk=effect.pk).update(status=EffectStatus.PENDING)
    drain(gateway)
    assert len(gateway.channels) == channels_before


@pytest.mark.django_db(transaction=True)
def test_results_post_is_not_repeated_after_a_crash(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    match = finish(to_in_progress(Match.objects.get()), "B")
    drain(gateway)
    effect = OutboxEffect.objects.get(kind="post_results", match=match)
    OutboxEffect.objects.filter(pk=effect.pk).update(status=EffectStatus.PENDING)
    drain(gateway)
    assert len([row for row in gateway.posted_files if row[0] == RESULTS_CHANNEL]) == 1


@pytest.mark.django_db(transaction=True)
def test_board_edit_after_channel_deleted_is_harmless(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    gateway.deleted.append(resource(match, ResourceKind.TEXT))
    lobby.mark_ready(match.pk, players[0])
    drain(gateway)
    assert OutboxEffect.objects.filter(kind="render_board", status=EffectStatus.SKIPPED).exists()


@pytest.mark.django_db(transaction=True)
def test_queue_message_is_created_once_then_edited(wired: Community) -> None:
    gateway = FakeGateway()
    request_queue(wired)
    drain(gateway)
    wired.refresh_from_db()
    first = wired.queue_message_id
    assert first
    request_queue(wired)
    drain(gateway)
    wired.refresh_from_db()
    assert wired.queue_message_id == first


@pytest.mark.django_db(transaction=True)
def test_a_join_while_the_queue_renders_gets_a_render_of_its_own(
    wired: Community, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render reads the queue before it edits Discord, so a join in between needs another."""
    gateway = FakeGateway()
    first, second = make_player(1), make_player(2)
    queue.join(wired.pk, first)
    read_queue = boards.queue_view

    def join_after_the_read(row: Community) -> boards.QueueView:
        view = read_queue(row)
        if view.size == 1:
            queue.join(wired.pk, second)
        return view

    monkeypatch.setattr(boards, "queue_view", join_after_the_read)
    drain(gateway)
    wired.refresh_from_db()
    assert "2/12" in str(gateway.views[wired.queue_message_id].to_components())
    assert OutboxEffect.objects.filter(kind="render_queue", status=EffectStatus.DONE).count() == 2


@pytest.mark.django_db(transaction=True)
def test_a_render_requested_before_one_starts_still_coalesces(wired: Community) -> None:
    request_queue(wired)
    request_queue(wired)
    assert OutboxEffect.objects.filter(kind="render_queue").count() == 1


@pytest.mark.django_db(transaction=True)
def test_unknown_effect_fails_without_stopping_others(wired: Community) -> None:
    gateway = FakeGateway()
    add_effect(wired, "does_not_exist")
    request_queue(wired)
    drain(gateway)
    assert OutboxEffect.objects.filter(kind="does_not_exist", status=EffectStatus.FAILED).exists()
    assert OutboxEffect.objects.filter(kind="render_queue", status=EffectStatus.DONE).exists()


@pytest.mark.django_db(transaction=True)
def test_reconcile_requests_renders_for_live_matches(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    for player in players:
        queue.join(wired.pk, player)
    gateway = FakeGateway()
    drain(gateway)
    assert effects.request_reconcile() == 2
    drain(gateway)


@pytest.mark.django_db(transaction=True)
def test_stale_channels_of_failed_cleanups_are_listed(wired: Community) -> None:
    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    lobby.staff_void(match.pk, None, "x")
    OutboxEffect.objects.filter(kind="cleanup_voice", match=match).update(
        status=EffectStatus.FAILED
    )
    listed = {row[1] for row in effects.stale_resources()}
    assert ResourceKind.LOBBY_VOICE in listed


# --- Real discord.py objects ---------------------------------------------------------------


def test_route_button_builds_on_real_discord_py() -> None:
    button = RouteButton("ready", 5, label="Ready", style=discord.ButtonStyle.success)
    assert button.custom_id == button_id("ready", 5)
    assert button.item.label == "Ready"


def test_player_helper_updates_display_name(db: None) -> None:
    get_or_create_player(10_000_500, "Old")
    assert get_or_create_player(10_000_500, "New").display_name == "New"


# --- The match channel: chat is free, and the flow stays in view -------------------------------


def channel_order(gateway: FakeGateway, match: Match) -> list[int]:
    return gateway.messages[resource(match, ResourceKind.TEXT)]


def live_notice_texts(gateway: FakeGateway, match: Match) -> list[str]:
    return [gateway.text_by_id[message_id] for message_id in effects.live_notices(match.pk)]


def assert_board_last(gateway: FakeGateway, match: Match) -> None:
    """The flow is at the bottom: the board, then the champion grid's messages in order."""
    order = channel_order(gateway, match)
    grid = [
        message_id
        for kind in (ResourceKind.PICKER_MESSAGE, ResourceKind.PICKER_MESSAGE_2)
        if (message_id := effects.resource_id(match.pk, kind)) is not None
    ]
    expected = [resource(match, ResourceKind.BOARD_MESSAGE), *grid]
    assert order[-len(expected) :] == expected


def popped_match(wired: Community, gateway: FakeGateway) -> Match:
    for player in [make_player(n) for n in range(1, 13)]:
        queue.join(wired.pk, player)
    drain(gateway)
    return Match.objects.get()


def run_due_board_moves(gateway: FakeGateway) -> None:
    """Skip the wait: make every chat-driven move due now, then run it."""
    OutboxEffect.objects.filter(kind="move_board", status=EffectStatus.PENDING).update(
        run_after=effects.now()
    )
    drain(gateway)


@pytest.mark.django_db(transaction=True)
def test_board_stays_last_and_only_current_pings_survive_a_whole_match(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    assert_board_last(gateway, match)
    assert live_notice_texts(gateway, match) == [
        "your lobby is ready. Press **Ready** on the board below."
    ]

    match = ready_all(match)
    drain(gateway)
    assert_board_last(gateway, match)
    assert live_notice_texts(gateway, match) == [
        "everyone's ready. Vote for up to two captains on the board."
    ]

    match = vote_captains(match)
    drain(gateway)
    assert_board_last(gateway, match)
    first = captain_of(match, match.player_first_pick).player.display_name
    captain_a = captain_of(match, "A").player.display_name
    captain_b = captain_of(match, "B").player.display_name
    assert live_notice_texts(gateway, match) == [
        f"the player draft is on. **{first}** picks first.",
        f"Captains voted in: **{captain_a}** (12 votes) and **{captain_b}** (12 votes).",
    ]

    match = run_player_draft(match)
    drain(gateway)
    assert_board_last(gateway, match)
    assert live_notice_texts(gateway, match) == [
        "teams are set. Claim a side: Hidden King bans and picks first."
    ]

    match = claim_sides(match)
    drain(gateway)
    assert_board_last(gateway, match)
    assert live_notice_texts(gateway, match) == [
        "you're Hidden King. You ban first. The champion draft is on."
    ]

    match = to_in_progress(match)
    drain(gateway)
    assert_board_last(gateway, match)
    assert live_notice_texts(gateway, match) == ["Lobby code: `ABC123`"]

    match = finish(match, "A")
    drain(gateway)
    assert live_notice_texts(gateway, match) == []
    assert channel_order(gateway, match) == [resource(match, ResourceKind.BOARD_MESSAGE)]


@pytest.mark.django_db(transaction=True)
def test_a_step_within_a_status_edits_the_board_in_place(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    board = resource(match, ResourceKind.BOARD_MESSAGE)
    lobby.mark_ready(match.pk, match.participants.first().player)
    drain(gateway)
    assert resource(match, ResourceKind.BOARD_MESSAGE) == board
    assert len(live_notice_texts(gateway, match)) == 1  # The ping must outlast one Ready click.


@pytest.mark.django_db(transaction=True)
def test_chat_survives_every_move_and_clear(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    text_id = resource(match, ResourceKind.TEXT)
    chat = [gateway.chat(text_id) for _ in range(5)]
    run_due_board_moves(gateway)
    match = ready_all(match)
    drain(gateway)
    chat.append(gateway.chat(text_id))
    match = vote_captains(match)
    drain(gateway)
    assert set(chat) <= set(channel_order(gateway, match))
    assert_board_last(gateway, match)


@pytest.mark.django_db(transaction=True)
def test_a_little_chat_waits_and_a_lot_moves_the_board_soon(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    text_id = resource(match, ResourceKind.TEXT)
    old_board = resource(match, ResourceKind.BOARD_MESSAGE)

    for _ in range(effects.BOARD_MOVE_AFTER_MESSAGES - 1):
        gateway.chat(text_id)
    move = OutboxEffect.objects.get(kind="move_board", match=match, status=EffectStatus.PENDING)
    wait = (move.run_after - effects.now()).total_seconds()
    assert wait > effects.BOARD_MOVE_MAX_WAIT_SECONDS
    drain(gateway)
    assert resource(match, ResourceKind.BOARD_MESSAGE) == old_board  # Not due yet.

    gateway.chat(text_id)
    move.refresh_from_db()
    assert (move.run_after - effects.now()).total_seconds() <= effects.BOARD_MOVE_QUIET_SECONDS

    run_due_board_moves(gateway)
    assert_board_last(gateway, match)
    assert resource(match, ResourceKind.BOARD_MESSAGE) != old_board
    assert old_board not in channel_order(gateway, match)  # Exactly one board on screen.


@pytest.mark.django_db(transaction=True)
def test_chat_waits_for_a_quiet_moment_but_not_for_ever(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    text_id = resource(match, ResourceKind.TEXT)
    for _ in range(effects.BOARD_MOVE_AFTER_MESSAGES):
        gateway.chat(text_id)
    move = OutboxEffect.objects.get(kind="move_board", match=match, status=EffectStatus.PENDING)
    planned = move.run_after
    gateway.chat(text_id)
    move.refresh_from_db()
    assert move.run_after > planned  # Still talking, so still waiting.

    limit = effects.now() + timedelta(seconds=1)
    move.payload["move_by"] = limit.isoformat()
    move.save(update_fields=["payload"])
    gateway.chat(text_id)
    move.refresh_from_db()
    assert move.run_after == limit  # Non-stop chat can't hold it off past the limit.


@pytest.mark.django_db(transaction=True)
def test_chat_from_before_a_move_no_longer_counts(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    text_id = resource(match, ResourceKind.TEXT)
    for _ in range(effects.BOARD_MOVE_AFTER_MESSAGES - 1):
        gateway.chat(text_id)
    match = ready_all(match)
    drain(gateway)
    assert_board_last(gateway, match)
    assert not OutboxEffect.objects.filter(kind="move_board", status=EffectStatus.PENDING).exists()

    gateway.chat(text_id)
    move = OutboxEffect.objects.get(kind="move_board", match=match, status=EffectStatus.PENDING)
    assert move.payload["messages"] == 1
    assert (move.run_after - effects.now()).total_seconds() > effects.BOARD_MOVE_MAX_WAIT_SECONDS


@pytest.mark.django_db(transaction=True)
def test_champion_grid_follows_the_board_and_goes_when_the_draft_ends(wired: Community) -> None:
    gateway = FakeGateway()
    match = to_champion_draft(popped_match(wired, gateway))
    drain(gateway)
    board = resource(match, ResourceKind.BOARD_MESSAGE)
    picker = resource(match, ResourceKind.PICKER_MESSAGE)
    picker_2 = resource(match, ResourceKind.PICKER_MESSAGE_2)
    assert_board_last(gateway, match)

    # A step with no chat below edits the board in place, and posts the grid afresh: an edited
    # grid message would carry an "(edited)" tag that splits the grid.
    step = build_plan(mode_of(match))[match.step_index]
    captain = captain_of(match, team_for_role(step.role, match.champion_first_pick)).player
    champion = boards.board_state(match.pk).champions[-1].id  # Alphabetically last.
    draft_service.choose_champion(match.pk, captain, match.step_index, champion)
    drain(gateway)
    assert resource(match, ResourceKind.BOARD_MESSAGE) == board
    assert board in gateway.edited
    new_grid = [
        resource(match, kind)
        for kind in (ResourceKind.PICKER_MESSAGE, ResourceKind.PICKER_MESSAGE_2)
    ]
    assert picker not in new_grid and picker_2 not in new_grid
    assert not set(new_grid) & gateway.edited
    assert {picker, picker_2}.isdisjoint(channel_order(gateway, fresh(match)))
    assert_board_last(gateway, match)
    assert any(
        button.get("disabled") for button in all_buttons(gateway.views[new_grid[1]].to_components())
    )
    picker = new_grid[0]

    # Chat below moves both, leaving one of each.
    text_id = resource(match, ResourceKind.TEXT)
    chat = [gateway.chat(text_id) for _ in range(effects.BOARD_MOVE_AFTER_MESSAGES)]
    run_due_board_moves(gateway)
    assert_board_last(gateway, match)
    order = channel_order(gateway, fresh(match))
    assert board not in order
    assert picker not in order
    assert set(chat) <= set(order)

    # Once the draft ends, the grid goes and the board is last again.
    match = run_champion_draft(match)
    drain(gateway)
    assert effects.resource_id(match.pk, ResourceKind.PICKER_MESSAGE) is None
    assert effects.resource_id(match.pk, ResourceKind.PICKER_MESSAGE_2) is None
    assert_board_last(gateway, match)
    assert not OutboxEffect.objects.filter(status=EffectStatus.FAILED).exists()


@pytest.mark.django_db(transaction=True)
def test_a_deleted_board_comes_back_above_the_picker(wired: Community) -> None:
    gateway = FakeGateway()
    match = to_champion_draft(popped_match(wired, gateway))
    drain(gateway)
    text_id = resource(match, ResourceKind.TEXT)
    old_picker = resource(match, ResourceKind.PICKER_MESSAGE)
    gateway.messages[text_id].remove(resource(match, ResourceKind.BOARD_MESSAGE))

    step = build_plan(mode_of(match))[match.step_index]
    captain = captain_of(match, team_for_role(step.role, match.champion_first_pick)).player
    champion = draft_service.available_champions(match)[0]
    draft_service.choose_champion(match.pk, captain, match.step_index, champion)
    drain(gateway)

    assert_board_last(gateway, match)
    assert old_picker not in channel_order(gateway, match)


@pytest.mark.django_db(transaction=True)
def test_a_state_change_brings_a_buried_board_down_straight_away(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    gateway.chat(resource(match, ResourceKind.TEXT))
    match = ready_all(match)
    drain(gateway)
    assert_board_last(gateway, match)


@pytest.mark.django_db(transaction=True)
def test_a_chat_move_after_the_board_already_moved_does_nothing(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    gateway.chat(resource(match, ResourceKind.TEXT))
    match = ready_all(match)
    drain(gateway)
    board = resource(match, ResourceKind.BOARD_MESSAGE)
    run_due_board_moves(gateway)
    assert resource(match, ResourceKind.BOARD_MESSAGE) == board
    assert OutboxEffect.objects.filter(kind="move_board", status=EffectStatus.SKIPPED).exists()


@pytest.mark.django_db(transaction=True)
def test_a_deleted_board_comes_back(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    text_id = resource(match, ResourceKind.TEXT)
    gateway.messages[text_id].remove(resource(match, ResourceKind.BOARD_MESSAGE))
    lobby.mark_ready(match.pk, match.participants.first().player)
    drain(gateway)
    assert_board_last(gateway, match)


@pytest.mark.django_db(transaction=True)
def test_chat_elsewhere_in_the_server_is_ignored(wired: Community) -> None:
    popped_match(wired, FakeGateway())
    effects.note_chat(QUEUE_CHANNEL)
    assert not OutboxEffect.objects.filter(kind="move_board").exists()


@pytest.mark.django_db(transaction=True)
def test_chat_in_a_closed_channel_is_ignored(wired: Community) -> None:
    match = popped_match(wired, FakeGateway())
    text_id = resource(match, ResourceKind.TEXT)
    effects.mark_resource_deleted(match.pk, ResourceKind.TEXT)
    effects.note_chat(text_id)
    assert not OutboxEffect.objects.filter(kind="move_board").exists()


@pytest.mark.django_db(transaction=True)
def test_a_new_lobby_host_replaces_the_first_hosts_prompt(wired: Community) -> None:
    gateway = FakeGateway()
    match = popped_match(wired, gateway)
    match = run_champion_draft(to_champion_draft(match))
    drain(gateway)
    first = effects.live_notices(match.pk)
    assert len(first) == 1
    draft_service.on_lobby_code_timeout(fresh(match))
    drain(gateway)
    second = effects.live_notices(match.pk)
    assert len(second) == 1
    assert second != first
    assert_board_last(gateway, match)


@pytest.mark.django_db(transaction=True)
def test_repeat_staff_requests_replace_each_other(wired: Community) -> None:
    gateway = FakeGateway()
    match = to_in_progress(popped_match(wired, gateway))
    results_service.on_in_progress_timeout(fresh(match))
    drain(gateway)
    assert "needs a decision" in live_notice_texts(gateway, match)[0]
    results_service.on_disputed_timeout(fresh(match))
    drain(gateway)
    notices = live_notice_texts(gateway, match)
    assert len(notices) == 1
    assert "still needs a decision" in notices[0]


@pytest.mark.django_db(transaction=True)
def test_a_failed_board_move_does_not_ping_twice(wired: Community) -> None:
    gateway = FakeGateway()
    match = ready_all(popped_match(wired, gateway))
    gateway.fail_next["send_view"] = discord.HTTPException(
        SimpleNamespace(status=500, reason="boom"), "boom"
    )
    drain(gateway)
    OutboxEffect.objects.filter(kind="render_board", status=EffectStatus.PENDING).update(
        run_after=effects.now()
    )
    drain(gateway)
    assert len([row for row in gateway.texts if "Vote for up to two captains" in row[1]]) == 1
    assert_board_last(gateway, match)


def test_only_other_peoples_server_messages_count_as_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []
    monkeypatch.setattr(effects, "note_chat", seen.append)
    bot = SimpleNamespace(user=SimpleNamespace(id=1))

    def message(guild: object, author: int, channel: int) -> Any:
        return SimpleNamespace(
            guild=guild, author=SimpleNamespace(id=author), channel=SimpleNamespace(id=channel)
        )

    async def scenario() -> None:
        await DraftBot.on_message(bot, message(object(), 1, 5))  # type: ignore[arg-type]
        await DraftBot.on_message(bot, message(None, 2, 6))  # type: ignore[arg-type]
        await DraftBot.on_message(bot, message(object(), 2, 7))  # type: ignore[arg-type]

    asyncio.run(scenario())
    assert seen == [7]


# --- Subs ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_paused_board_drops_the_grid_and_fits_with_two_sub_requests(popped: Match) -> None:
    from league.services import subs

    match = to_champion_draft(popped)
    leaving = match.participants.filter(team="A", is_captain=False).first().player
    other = match.participants.filter(team="B", is_captain=False).first().player
    subs.request_sub_out(match.pk, captain_of(match, "B").player, other.pk)
    subs.request_sub_out(match.pk, leaving, leaving.pk)
    state = boards.board_state(match.pk)
    view = render_board(state)
    assert_within_limits(view)
    ids = [custom_id.split(":")[2] for custom_id in all_custom_ids(view.to_components())]
    assert "choose_champion" not in ids
    assert {"sub_approve", "sub_reject", "sub_invite_open", "sub_queue", "sub_cancel"} <= set(ids)
    assert all(action in registered_actions() for action in ids)
    assert render_pickers(state) == []
    assert "Paused for a sub" in str(view.to_components())


@pytest.mark.django_db
def test_live_champion_board_still_fits_with_the_sub_button(popped: Match) -> None:
    match = to_champion_draft(popped)
    view = render_board(boards.board_state(match.pk))
    assert_within_limits(view)
    assert button_id("sub_open", match.pk) in str(view.to_components())


def test_sub_offer_dm_says_who_asked_and_has_both_answers() -> None:
    from datetime import UTC, datetime

    from bot.renderers.board import render_sub_offer_dm
    from league.services.subs import OfferInfo

    info = OfferInfo(
        offer_id=9,
        match_id=5,
        pending=True,
        discord_user_id=10_000_077,
        player_id=77,
        leaving_name="Fake 3",
        team="A",
        status=Status.IN_PROGRESS,
        source="invite",
        invited_by="MDMT",
        deadline_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    view = render_sub_offer_dm(info, "MDMT's Team")
    payload = str(view.to_components())
    assert "**MDMT** asked you" in payload
    assert "take this team's result" in payload
    assert button_id("sub_accept", 5, 9) in payload
    assert button_id("sub_decline", 5, 9) in payload
    assert_within_limits(view)


@pytest.mark.django_db(transaction=True)
def test_sub_offer_is_dmd_and_closed_dms_move_down_the_queue(wired: Community) -> None:
    from league.models import SubOffer
    from league.models.subs import OfferStatus
    from league.services import subs

    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    waiting = [make_player(n) for n in (30, 31)]
    for player in waiting:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    request = subs.request_sub_out(match.pk, players[3], players[3].pk)
    gateway.closed_dms.add(waiting[0].discord_user_id)
    subs.offer_to_queue(request.pk, players[3])
    drain(gateway)
    assert SubOffer.objects.get(player=waiting[0]).status == OfferStatus.FAILED
    assert SubOffer.objects.get(player=waiting[1]).status == OfferStatus.PENDING
    sent = [view for user_id, view in gateway.dms if user_id == waiting[1].discord_user_id]
    assert sent and "sub_accept" in str(sent[-1].to_components())
    assert any("couldn't DM" in row[1] for row in gateway.texts)


@pytest.mark.django_db(transaction=True)
def test_sub_in_waiting_room_is_moved_to_their_teams_voice(wired: Community) -> None:
    from league.services import subs

    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = to_champion_draft(Match.objects.get())
    drain(gateway)
    leaving = match.participants.filter(team="B", is_captain=False).first().player
    sub = make_player(77)
    gateway.voice[WAITING_ROOM] = {sub.discord_user_id}
    request = subs.request_sub_out(match.pk, leaving, leaving.pk)
    offer = subs.invite_sub(request.pk, leaving, sub.discord_user_id, sub.display_name)
    subs.answer_offer(offer.pk, sub, True)
    drain(gateway)
    team_b = resource(match, ResourceKind.TEAM_B_VOICE)
    text_id = resource(match, ResourceKind.TEXT)
    assert sub.discord_user_id in gateway.voice[team_b]
    assert (text_id, sub.discord_user_id) in gateway.granted
    assert (text_id, leaving.discord_user_id) in gateway.revoked
    assert not OutboxEffect.objects.filter(status=EffectStatus.FAILED).exists()


@pytest.mark.django_db(transaction=True)
def test_sub_not_in_voice_gets_a_dm_with_voice_and_channel_links(wired: Community) -> None:
    from league.services import subs

    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    drain(gateway)
    match = Match.objects.get()
    sub = make_player(77)
    request = subs.request_sub_out(match.pk, players[3], players[3].pk)
    subs.answer_offer(
        subs.invite_sub(request.pk, players[3], sub.discord_user_id, sub.display_name).pk,
        sub,
        True,
    )
    drain(gateway)
    sent = [view for user_id, view in gateway.dms if user_id == sub.discord_user_id]
    payload = str(sent[-1].to_components())
    assert "You're subbing in" in payload
    assert "https://discord.com/channels/999/" in payload
    assert "https://discord.gg/invite-" in payload


@pytest.mark.django_db(transaction=True)
def test_fake_players_in_the_queue_take_a_sub_spot(wired: Community, settings) -> None:
    from league.services import subs

    players = [make_player(n) for n in range(1, 13)]
    gateway = FakeGateway()
    for player in players:
        queue.join(wired.pk, player)
    fake = dev.fake_players(1)[0]
    queue.join(wired.pk, fake)
    drain(gateway)
    match = Match.objects.get()
    request = subs.request_sub_out(match.pk, players[3], players[3].pk)
    subs.offer_to_queue(request.pk, players[3])
    drain(gateway)
    assert fresh(match).participants.filter(player=fake, is_active=True).exists()


def test_ready_dm_links_the_match_channel() -> None:
    view = render_ready_dm(
        5, "https://discord.gg/abc", heading="Your match is ready", channel_url="https://x/y"
    )
    assert "https://x/y" in str(view.to_components())
    assert_within_limits(view)


@pytest.mark.django_db
@pytest.mark.parametrize("phase", ["champion_draft", "in_progress"])
def test_board_fits_with_the_most_sub_requests_allowed(popped: Match, phase: str) -> None:
    from league.services import subs

    match = to_champion_draft(popped)
    if phase == "in_progress":
        match = to_in_progress(match)
    team_a = list(match.participants.filter(team="A", is_captain=False).order_by("id"))
    captain_a = captain_of(match, "A").player
    subs.request_sub_out(match.pk, captain_a, team_a[0].player_id)  # Waiting on approval.
    for row in team_a[1:4]:
        subs.request_sub_out(match.pk, row.player, row.player_id)
    state = boards.board_state(match.pk)
    assert len(state.subs) == subs.MAX_PENDING
    assert_within_limits(render_board(state))


# --- Vote, sides and the draft summary ---------------------------------------------------------


def _views(match: Match) -> list[Any]:
    state = boards.board_state(match.pk)
    return [render_board(state), *render_pickers(state)]


def _check(match: Match) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for view in _views(match):
        assert_within_limits(view)
        payload = view.to_components()
        for custom_id in all_custom_ids(payload):
            assert custom_id.split(":")[2] in registered_actions(), custom_id
        components.extend(payload)
    return components


@pytest.mark.django_db
def test_vote_board_has_a_two_choice_select(voting: Match, players: list[Player]) -> None:
    lobby.cast_captain_votes(voting.pk, players[0], [players[1].pk])
    text = str(_check(voting))
    assert "1/12 voted" in text
    assert "captain_vote" in text
    assert "'max_values': 2" in text


@pytest.mark.django_db
def test_side_board_has_both_claim_buttons(voting: Match) -> None:
    match = run_player_draft(vote_captains(voting))
    labels = [button.get("label") for button in all_buttons(_check(match))]
    assert "Become Hidden King captain" in labels
    assert "Become Archmother captain" in labels


@pytest.mark.django_db
def test_champion_board_has_one_random_button(voting: Match) -> None:
    labels = [button.get("label") for button in all_buttons(_check(to_champion_draft(voting)))]
    assert labels.count("Random") == 1


@pytest.mark.django_db
def test_random_choice_is_marked_on_the_board(voting: Match) -> None:
    match = to_champion_draft(voting)
    draft_service.choose_random_champion(match.pk, acting_captain(match), match.step_index)
    assert "🎲" in str(_check(fresh(match)))


@pytest.mark.django_db
def test_finished_draft_shows_its_name_gif_and_glhf(voting: Match) -> None:
    from league.domain.engine import draft_name

    match = to_champion_draft(voting)
    for _ in range(16):
        match = fresh(match)
        draft_service.choose_random_champion(match.pk, acting_captain(match), match.step_index)
    match = fresh(match)
    assert match.status == Status.LOBBY_CODE
    text = str(_check(match))
    assert draft_name(match.rng_seed) in text
    assert "GLHF" in text
    assert "tenor.com" in text  # The GIF, from presets/draft_gifs.json.


@pytest.mark.django_db
def test_side_board_with_four_pending_subs_stays_within_limits(voting: Match) -> None:
    from league.services import subs

    match = run_player_draft(vote_captains(voting))
    for leaving in match.participants.filter(is_captain=False, is_active=True)[:4]:
        subs.request_sub_out(match.pk, leaving.player, leaving.player_id)
    _check(fresh(match))
