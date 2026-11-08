"""Click and command handlers end to end, with fake interactions but the real router and database.

These catch wiring mistakes that only appear once Discord calls the code: wrong argument order, a
modal opened after a defer, a refusal shown to the wrong person.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands
from django.db import connections

from bot.client import DraftBot
from bot.commands import player as player_commands
from bot.commands import setup as setup_commands
from bot.commands import staff
from bot.db import db
from bot.handlers.draft import LobbyCodeModal
from bot.handlers.results import DisputeModal, ReportModal, process_report
from bot.routing import Context, _dispatch
from league.domain.engine import Status
from league.domain.errors import InvalidAction
from league.models import Community, DraftModeVersion, Match, Player, QueueEntry, ResultSubmission
from league.services import community, integrity, queue, ratings
from league.services.common import captain_of, open_season
from tests.conftest import make_player
from tests.helpers import fresh, keep_captains, ready_all, run_champion_draft, run_player_draft

GUILD_ID = 999
ARCHIVE_CHANNEL = 7004
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def wired(transactional_db: None) -> Iterator[Community]:
    from django.conf import settings

    from league.services import champions, modes

    presets = settings.BASE_DIR / "presets"
    champions.seed_from_file(presets / "champions.yaml")
    for name in ("threeban_1win_2026.yaml", "dcs_s3_2ban.yaml"):
        modes.publish(modes.load_yaml(presets / name))
    row = community.ensure(guild_id=GUILD_ID, name="Test")
    community.save_wiring(row.pk, archive_channel_id=ARCHIVE_CHANNEL)
    row.refresh_from_db()
    yield row
    asyncio.run(db(connections.close_all))


class FakeResponse:
    def __init__(self) -> None:
        self.done = False
        self.modal: discord.ui.Modal | None = None
        self.deferred: dict[str, Any] | None = None
        self.edited: dict[str, Any] | None = None

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: Any) -> None:
        assert not self.done, "An interaction can only be responded to once"
        self.done = True
        self.deferred = kwargs

    async def send_modal(self, modal: discord.ui.Modal) -> None:
        assert not self.done, "A modal must be the first response: it can't follow a defer"
        self.done = True
        self.modal = modal

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        assert not self.done
        self.done = True
        self.message = (args, kwargs)

    async def edit_message(self, **kwargs: Any) -> None:
        assert not self.done
        self.done = True
        self.edited = kwargs


def fake_message() -> MagicMock:
    message = MagicMock()
    message.id = 8001
    attachment = MagicMock()
    attachment.id = 8002
    message.attachments = [attachment]
    return message


def fake_interaction(
    player: Player,
    *,
    guild: bool = True,
    administrator: bool = False,
    role_ids: tuple[int, ...] = (),
) -> MagicMock:
    user = MagicMock(spec=discord.Member)
    user.id = player.discord_user_id
    user.display_name = player.display_name
    user.guild_permissions = MagicMock(administrator=administrator)
    user.roles = [MagicMock(id=role_id) for role_id in role_ids]
    interaction = MagicMock()
    interaction.id = abs(hash((player.pk, id(user)))) % 10**15
    interaction.user = user
    interaction.guild_id = GUILD_ID if guild else None
    interaction.response = FakeResponse()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.client = MagicMock()
    archive = MagicMock()
    archive.send = AsyncMock(return_value=fake_message())
    interaction.client.get_channel = MagicMock(return_value=archive)
    interaction.client.wake_effects = MagicMock()
    interaction.archive = archive
    return interaction


def click(
    interaction: MagicMock,
    action: str,
    match_id: int = 0,
    arg: str = "",
    values: tuple[str, ...] = (),
) -> None:
    asyncio.run(_dispatch(Context(interaction, action, match_id, arg, values)))


def replies(interaction: MagicMock) -> list[str]:
    return [call.args[0] for call in interaction.followup.send.call_args_list if call.args]


def attachment(data: bytes = PNG, size: int | None = None) -> MagicMock:
    item = MagicMock(spec=discord.Attachment)
    item.size = len(data) if size is None else size
    item.read = AsyncMock(return_value=data)
    return item


def lobby_of_twelve(wired: Community) -> tuple[Match, list[Player]]:
    players = [make_player(n) for n in range(1, 13)]
    for player in players:
        queue.join(wired.pk, player)
    return Match.objects.get(), players


# --- Queue -----------------------------------------------------------------------------------


def test_join_and_leave_buttons(wired: Community) -> None:
    player = make_player(1)
    interaction = fake_interaction(player)
    click(interaction, "join")
    assert QueueEntry.objects.filter(player=player).exists()
    assert replies(interaction) == []  # The queue board updates: there's nothing else to say.
    interaction.client.wake_effects.assert_called()

    interaction = fake_interaction(player)
    click(interaction, "leave")
    assert not QueueEntry.objects.filter(player=player).exists()
    assert replies(interaction) == []


def test_leaving_when_not_queued_still_gets_a_reply(wired: Community) -> None:
    interaction = fake_interaction(make_player(1))
    click(interaction, "leave")
    assert replies(interaction)
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


def test_refusal_is_shown_to_the_clicker(wired: Community) -> None:
    player = make_player(1)
    click(fake_interaction(player), "join")
    interaction = fake_interaction(player)
    click(interaction, "join")
    assert "already in the queue" in replies(interaction)[0]
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True


def test_unexpected_errors_never_leak_details(
    wired: Community, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any) -> None:
        raise RuntimeError("secret database detail")

    monkeypatch.setattr(queue, "join", explode)
    interaction = fake_interaction(make_player(1))
    click(interaction, "join")
    assert replies(interaction) == ["Something went wrong on our side. Please try again."]


def test_unset_server_gets_a_clear_message(transactional_db: None) -> None:
    interaction = fake_interaction(make_player(1))
    click(interaction, "join")
    assert "run /setup" in replies(interaction)[0]
    asyncio.run(db(connections.close_all))


def test_stale_custom_id_action_is_handled(wired: Community) -> None:
    interaction = fake_interaction(make_player(1))
    click(interaction, "no_such_action")
    assert interaction.response.message[0][0] == "That button is no longer available."


# --- Lobby -----------------------------------------------------------------------------------


def test_ready_button_works_in_guild_and_dm(wired: Community) -> None:
    match, players = lobby_of_twelve(wired)
    interaction = fake_interaction(players[0])
    click(interaction, "ready", match.pk)
    assert replies(interaction) == []  # On the board there's nothing else to say.
    assert match.participants.get(player=players[0]).ready_at is not None

    dm = fake_interaction(players[1], guild=False)
    click(dm, "ready", match.pk)
    assert "You're ready" in replies(dm)[0]


def test_handoff_buttons_full_journey(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = ready_all(match)
    captain = captain_of(match, "A").player
    target = match.participants.filter(is_captain=False).first().player

    opener = fake_interaction(captain)
    click(opener, "handoff_open", match.pk)
    view = opener.followup.send.call_args.kwargs["view"]
    buttons = [
        c
        for row in view.to_components()[0]["components"]
        for c in row.get("components", [])
        if c.get("type") == 2
    ]
    assert len(buttons) == 10
    assert all(b["custom_id"].split(":")[2] == "handoff_select" for b in buttons)
    assert target.display_name in [b["label"] for b in buttons]

    chooser = fake_interaction(captain)
    click(chooser, "handoff_select", match.pk, str(target.pk))
    handoff = match.handoffs.get()

    answer = fake_interaction(target)
    click(answer, "handoff_accept", match.pk, str(handoff.pk))
    assert captain_of(fresh(match), "A").player == target


def test_non_captain_cannot_open_handoff(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = ready_all(match)
    other = match.participants.filter(is_captain=False).first().player
    interaction = fake_interaction(other)
    click(interaction, "handoff_open", match.pk)
    assert "Only a captain" in replies(interaction)[0]


def test_keep_button(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = ready_all(match)
    for team in ("A", "B"):
        click(fake_interaction(captain_of(match, team).player), "keep", match.pk)
    assert fresh(match).status == Status.PLAYER_DRAFT


# --- Draft -----------------------------------------------------------------------------------


def test_player_and_champion_buttons(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = keep_captains(ready_all(match))
    captain = captain_of(match, match.player_first_pick).player
    target = match.participants.filter(team="").first().player_id
    click(fake_interaction(captain), "pick_player", match.pk, f"0.{target}")
    assert fresh(match).step_index == 1

    match = run_player_draft(match)
    from league.domain.draft_plan import build_plan, team_for_role
    from league.services.common import mode_of

    step = build_plan(mode_of(match))[match.step_index]
    acting = captain_of(match, team_for_role(step.role, match.champion_first_pick)).player
    champion = match.champion_pool[0]
    click(fake_interaction(acting), "choose_champion", match.pk, f"{match.step_index}.{champion}")
    assert fresh(match).draft_actions.filter(target_champion_id=champion).exists()

    match = fresh(match)
    step = build_plan(mode_of(match))[match.step_index]
    other = captain_of(match, team_for_role(step.role, match.champion_first_pick)).player
    second = match.champion_pool[1]
    click(
        fake_interaction(other), "pick_champion", match.pk, f"{match.step_index}.1", (str(second),)
    )  # The dropdown fallback for an oversized roster reaches the same place.
    assert fresh(match).draft_actions.filter(target_champion_id=second).exists()


def test_a_champion_click_out_of_turn_is_refused(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = run_player_draft(keep_captains(ready_all(match)))
    from league.domain.draft_plan import build_plan, team_for_role
    from league.services.common import mode_of

    step = build_plan(mode_of(match))[match.step_index]
    acting = team_for_role(step.role, match.champion_first_pick)
    waiting = captain_of(match, "B" if acting == "A" else "A").player
    champion = match.champion_pool[0]
    interaction = fake_interaction(waiting)
    click(interaction, "choose_champion", match.pk, f"{match.step_index}.{champion}")
    assert "not your turn" in replies(interaction)[0]
    assert not fresh(match).draft_actions.filter(target_champion_id=champion).exists()


def test_a_stale_click_gets_a_friendly_reply(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = keep_captains(ready_all(match))
    captain = captain_of(match, match.player_first_pick).player
    target = match.participants.filter(team="").first().player_id
    click(fake_interaction(captain), "pick_player", match.pk, f"0.{target}")
    late = fake_interaction(captain)
    click(late, "pick_player", match.pk, f"0.{target}")
    assert "Too late" in replies(late)[0]


def test_lobby_code_modal_journey(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = run_champion_draft(run_player_draft(keep_captains(ready_all(match))))
    opener = fake_interaction(match.lobby_code_host)
    click(opener, "code_open", match.pk)
    modal = opener.response.modal
    assert isinstance(modal, LobbyCodeModal)
    modal.to_components()

    modal.code._value = "XYZ-123"
    submit = fake_interaction(match.lobby_code_host)
    asyncio.run(modal.on_submit(submit))
    match = fresh(match)
    assert match.status == Status.IN_PROGRESS
    assert match.lobby_code == "XYZ-123"


def test_wrong_person_cannot_open_lobby_code(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    match = run_champion_draft(run_player_draft(keep_captains(ready_all(match))))
    other = match.participants.filter(is_captain=True).exclude(player=match.lobby_code_host).first()
    interaction = fake_interaction(other.player)
    click(interaction, "code_open", match.pk)
    assert interaction.response.modal is None
    assert "Only the chosen host" in interaction.response.message[0][0]


# --- Results ---------------------------------------------------------------------------------


def in_progress(wired: Community) -> Match:
    from tests.helpers import to_in_progress

    match, _players = lobby_of_twelve(wired)
    return to_in_progress(match)


def test_report_modal_journey(wired: Community) -> None:
    match = in_progress(wired)
    captain = captain_of(match, "A").player
    opener = fake_interaction(captain)
    click(opener, "report_open", match.pk, "A")
    modal = opener.response.modal
    assert isinstance(modal, ReportModal)
    assert modal.title == f"{captain.display_name}'s Team won"  # The press already said who won.
    assert modal.winner_select is None

    # discord.py fills these from the submitted payload; stand in for the filled components.
    modal.screenshot = MagicMock(values=[attachment()])
    modal.deadlock_id = MagicMock(value="122069248")
    submit = fake_interaction(captain)
    asyncio.run(modal.on_submit(submit))

    match = fresh(match)
    assert match.status == Status.AWAITING_CONFIRMATION
    row = ResultSubmission.objects.get(match=match)
    assert row.deadlock_match_id == 122069248
    assert row.archive_message_id == 8001
    assert row.attachment_id == 8002
    assert len(row.image_sha256) == 64
    sent = submit.archive.send.call_args
    assert sent.kwargs["file"].filename.endswith(".png")
    assert "other captain" in replies(submit)[0]


def test_add_my_screenshot_still_asks_who_won(wired: Community) -> None:
    match = in_progress(wired)
    captain_a = captain_of(match, "A").player
    captain_b = captain_of(match, "B").player
    asyncio.run(
        process_report(fake_interaction(captain_a), match.pk, captain_a, "A", attachment(), "")
    )
    opener = fake_interaction(captain_b)
    click(opener, "report_open", match.pk)
    modal = opener.response.modal
    assert isinstance(modal, ReportModal)
    assert modal.winner_select is not None
    assert f"{captain_b.display_name}'s Team" in str(modal.to_components())

    modal.winner_select = MagicMock(values=["B"])
    modal.screenshot = MagicMock(values=[attachment()])
    modal.deadlock_id = MagicMock(value="")
    asyncio.run(modal.on_submit(fake_interaction(captain_b)))
    assert fresh(match).status == Status.DISPUTED  # Different winners go to staff.


@pytest.mark.parametrize(
    ("file", "message"),
    [
        (attachment(b"<svg></svg>"), "isn't a PNG"),
        (attachment(PNG, size=25 * 1024 * 1024), "too large"),
    ],
)
def test_bad_screenshots_are_refused_and_not_archived(
    wired: Community, file: MagicMock, message: str
) -> None:
    match = in_progress(wired)
    captain = captain_of(match, "A").player
    interaction = fake_interaction(captain)

    async def run() -> None:
        with pytest.raises(InvalidAction, match=message):
            await process_report(interaction, match.pk, captain, "A", file, "")

    asyncio.run(run())
    interaction.archive.send.assert_not_called()
    assert fresh(match).status == Status.IN_PROGRESS


def test_non_captain_cannot_open_report(wired: Community) -> None:
    match = in_progress(wired)
    other = match.participants.filter(is_captain=False).first().player
    interaction = fake_interaction(other)
    click(interaction, "report_open", match.pk)
    assert interaction.response.modal is None


def test_confirm_and_dispute_buttons(wired: Community) -> None:
    match = in_progress(wired)
    captain_a = captain_of(match, "A").player
    captain_b = captain_of(match, "B").player
    asyncio.run(
        process_report(fake_interaction(captain_a), match.pk, captain_a, "A", attachment(), "")
    )
    click(fake_interaction(captain_b), "confirm", match.pk)
    match = fresh(match)
    assert match.status == Status.COMPLETED
    assert integrity.check_all() == []


def test_dispute_modal_then_staff_buttons(wired: Community) -> None:
    match = in_progress(wired)
    captain_a = captain_of(match, "A").player
    captain_b = captain_of(match, "B").player
    asyncio.run(
        process_report(fake_interaction(captain_a), match.pk, captain_a, "A", attachment(), "")
    )
    opener = fake_interaction(captain_b)
    click(opener, "dispute_open", match.pk)
    modal = opener.response.modal
    assert isinstance(modal, DisputeModal)
    modal.note._value = "We won"
    asyncio.run(modal.on_submit(fake_interaction(captain_b)))
    assert fresh(match).status == Status.DISPUTED

    outsider = fake_interaction(make_player(90))
    click(outsider, "resolve_b", match.pk)
    assert "Only staff" in replies(outsider)[0]
    assert fresh(match).status == Status.DISPUTED

    admin = fake_interaction(make_player(91), administrator=True)
    click(admin, "resolve_b", match.pk)
    match = fresh(match)
    assert match.status == Status.COMPLETED
    assert match.winner == "B"


def test_staff_role_counts_as_staff(wired: Community) -> None:
    Community.objects.filter(pk=wired.pk).update(staff_role_id=555)
    match = in_progress(wired)
    captain_a = captain_of(match, "A").player
    captain_b = captain_of(match, "B").player
    asyncio.run(
        process_report(fake_interaction(captain_a), match.pk, captain_a, "A", attachment(), "")
    )
    asyncio.run(
        process_report(fake_interaction(captain_b), match.pk, captain_b, "B", attachment(), "")
    )
    assert fresh(match).status == Status.DISPUTED
    staffer = fake_interaction(make_player(92), role_ids=(555,))
    click(staffer, "resolve_void", match.pk)
    assert fresh(match).status == Status.VOIDED


# --- Slash commands --------------------------------------------------------------------------


def run_command(
    command: app_commands.Command[Any, Any, Any], interaction: MagicMock, **kwargs: Any
) -> None:
    asyncio.run(command.callback(interaction, **kwargs))


def member(player: Player) -> MagicMock:
    item = MagicMock(spec=discord.Member)
    item.id = player.discord_user_id
    item.display_name = player.display_name
    return item


def subcommand(group: app_commands.Group, name: str) -> app_commands.Command[Any, Any, Any]:
    command = group.get_command(name)
    assert isinstance(command, app_commands.Command)
    return command


def test_stats_and_leaderboard_commands(wired: Community) -> None:
    player = make_player(1)
    interaction = fake_interaction(player)
    run_command(player_commands.stats_command, interaction)
    assert "1000" in replies(interaction)[0]
    interaction = fake_interaction(player)
    run_command(player_commands.leaderboard_command, interaction)
    assert "10 games" in replies(interaction)[0]


def test_queue_status_command(wired: Community) -> None:
    player = make_player(1)
    queue.join(wired.pk, player)
    interaction = fake_interaction(player)
    run_command(subcommand(player_commands.queue_group, "status"), interaction)
    assert "#1 of 1" in replies(interaction)[0]


def test_submit_command_is_the_upload_fallback(wired: Community) -> None:
    match = in_progress(wired)
    captain = captain_of(match, "B").player
    interaction = fake_interaction(captain)
    run_command(
        player_commands.submit_command,
        interaction,
        winner=player_commands.TEAM_CHOICES[0],
        screenshot=attachment(),
        deadlock_match_id="",
    )
    match = fresh(match)
    assert match.status == Status.AWAITING_CONFIRMATION
    assert match.submissions.get().claimed_winner == "B"


def test_submit_command_refuses_non_captains(wired: Community) -> None:
    interaction = fake_interaction(make_player(1))
    run_command(
        player_commands.submit_command,
        interaction,
        winner=player_commands.TEAM_CHOICES[0],
        screenshot=attachment(),
        deadlock_match_id="",
    )
    assert "not a captain" in replies(interaction)[0]


def test_staff_commands_refuse_non_staff(wired: Community) -> None:
    target = make_player(2)
    interaction = fake_interaction(make_player(1))
    run_command(
        subcommand(player_commands.rating_group, "seed"),
        interaction,
        player=member(target),
        rating=2000,
    )
    assert "Only staff" in replies(interaction)[0]
    assert open_season(wired).ratings.count() == 0


def test_seed_and_adjust_commands(wired: Community) -> None:
    admin = make_player(1)
    target = make_player(2)
    interaction = fake_interaction(admin, administrator=True)
    run_command(
        subcommand(player_commands.rating_group, "seed"),
        interaction,
        player=member(target),
        rating=1400,
    )
    assert "now 1400" in replies(interaction)[0]
    interaction = fake_interaction(admin, administrator=True)
    run_command(
        subcommand(player_commands.rating_group, "adjust"),
        interaction,
        player=member(target),
        delta=-50,
        reason="Conduct",
    )
    assert "now 1350" in replies(interaction)[0]
    assert integrity.check_all() == []


def test_void_live_needs_confirm_then_applies(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    admin = make_player(50)
    interaction = fake_interaction(admin, administrator=True)
    run_command(
        subcommand(staff.match_group, "void"), interaction, match_id=match.pk, reason="Test"
    )
    assert fresh(match).status == Status.READY_CHECK  # Nothing happens before Confirm.
    view = interaction.followup.send.call_args.kwargs["view"]
    assert isinstance(view, staff.ConfirmView)

    confirmed = fake_interaction(admin, administrator=True)
    asyncio.run(view.confirm.callback(confirmed))
    assert fresh(match).status == Status.VOIDED


def test_rescore_finished_match_previews_then_corrects(wired: Community) -> None:
    from tests.helpers import finish

    match = finish(in_progress(wired), "A")
    admin = make_player(50)
    interaction = fake_interaction(admin, administrator=True)
    run_command(
        subcommand(staff.match_group, "rescore"),
        interaction,
        match_id=match.pk,
        winning_captain=member(captain_of(match, "B").player),
        reason="Wrong report",
    )
    assert fresh(match).winner == "A"
    assert "ledger rows" in interaction.followup.send.call_args.args[0]
    view = interaction.followup.send.call_args.kwargs["view"]
    asyncio.run(view.confirm.callback(fake_interaction(admin, administrator=True)))
    assert fresh(match).winner == "B"
    assert integrity.check_all() == []


def test_cancel_writes_nothing(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    admin = make_player(50)
    interaction = fake_interaction(admin, administrator=True)
    run_command(
        subcommand(staff.match_group, "void"), interaction, match_id=match.pk, reason="Test"
    )
    view = interaction.followup.send.call_args.kwargs["view"]
    asyncio.run(view.cancel.callback(fake_interaction(admin, administrator=True)))
    assert fresh(match).status == Status.READY_CHECK


def test_match_sub_command_live(wired: Community) -> None:
    match, players = lobby_of_twelve(wired)
    admin = make_player(50)
    sub = make_player(60)
    interaction = fake_interaction(admin, administrator=True)
    run_command(
        subcommand(staff.match_group, "sub"),
        interaction,
        match_id=match.pk,
        out=member(players[0]),
        replacement=member(sub),
        reason="Disconnected",
    )
    assert match.participants.filter(player=sub, is_active=True).exists()


def test_match_explain_and_status(wired: Community) -> None:
    match, _players = lobby_of_twelve(wired)
    admin = make_player(50)
    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(staff.match_group, "status"), interaction, match_id=match.pk)
    assert "ready_check" in replies(interaction)[0]
    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(staff.match_group, "explain"), interaction, match_id=match.pk)
    assert "new -> ready_check" in replies(interaction)[0]


def test_unknown_match_is_a_clear_refusal(wired: Community) -> None:
    interaction = fake_interaction(make_player(50), administrator=True)
    run_command(subcommand(staff.match_group, "status"), interaction, match_id=999999)
    assert "no match #999999" in replies(interaction)[0]


def test_mode_commands(wired: Community) -> None:
    admin = make_player(50)
    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(staff.mode_group, "list"), interaction)
    assert "threeban_1win_2026" in replies(interaction)[0]
    assert "(active)" in replies(interaction)[0]

    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(staff.mode_group, "activate"), interaction, slug="dcs_s3_2ban")
    wired.refresh_from_db()
    assert wired.active_mode == DraftModeVersion.objects.get(slug="dcs_s3_2ban")

    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(staff.mode_group, "activate"), interaction, slug="nope")
    assert "no mode called" in replies(interaction)[0]


def test_queue_kick_and_clear_commands(wired: Community) -> None:
    admin = make_player(50)
    target = make_player(1)
    queue.join(wired.pk, target)
    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(player_commands.queue_group, "kick"), interaction, player=member(target))
    assert replies(interaction) == ["Removed."]
    queue.join(wired.pk, target)
    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(player_commands.queue_group, "clear"), interaction)
    assert QueueEntry.objects.count() == 0


def test_penalty_commands(wired: Community) -> None:
    admin = make_player(50)
    target = make_player(1)
    interaction = fake_interaction(admin, administrator=True)
    run_command(
        subcommand(staff.penalty_group, "add"),
        interaction,
        player=member(target),
        kind=app_commands.Choice(name="Manual", value="manual"),
        reason="Test",
        minutes=30,
    )
    with pytest.raises(InvalidAction, match="cooldown"):
        queue.join(wired.pk, target)
    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(staff.penalty_group, "revoke"), interaction, player=member(target))
    queue.join(wired.pk, target)


def test_roster_sync_command_previews_then_applies(
    wired: Community, monkeypatch: pytest.MonkeyPatch
) -> None:
    from league.models import Champion
    from league.services import champions

    current = [
        champions.RemoteChampion(id=c.pk, name=c.name, icon_url="") for c in Champion.objects.all()
    ]
    monkeypatch.setattr(
        champions,
        "fetch_remote",
        lambda: [*current, champions.RemoteChampion(id=200, name="Newcomer", icon_url="")],
    )
    admin = make_player(50)
    interaction = fake_interaction(admin, administrator=True)
    run_command(subcommand(staff.roster_group, "sync"), interaction)
    assert "New: Newcomer" in interaction.followup.send.call_args.args[0]
    assert not Champion.objects.filter(pk=200).exists()
    view = interaction.followup.send.call_args.kwargs["view"]
    asyncio.run(view.confirm.callback(fake_interaction(admin, administrator=True)))
    assert Champion.objects.get(pk=200).is_enabled


def test_ledger_check_command(wired: Community) -> None:
    ratings.seed_player(open_season(wired), make_player(1), 1100, actor=None)
    interaction = fake_interaction(make_player(50), administrator=True)
    run_command(staff.ledger_check, interaction)
    assert replies(interaction) == ["Ledger OK."]


# --- Registration ----------------------------------------------------------------------------


def test_every_command_serialises_for_discord() -> None:
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    for command in (
        player_commands.leaderboard_command,
        player_commands.stats_command,
        player_commands.submit_command,
        player_commands.queue_group,
        player_commands.rating_group,
        setup_commands.setup_command,
        *staff.GROUPS,
        *staff.COMMANDS,
    ):
        tree.add_command(command)
    payloads = [command.to_dict(tree) for command in tree.get_commands()]
    names = {payload["name"] for payload in payloads}
    assert {"queue", "rating", "match", "penalty", "mode", "roster", "setup", "submit"} <= names
    rating = next(payload for payload in payloads if payload["name"] == "rating")
    assert {option["name"] for option in rating["options"]} == {"history", "seed", "adjust"}
    queue_payload = next(payload for payload in payloads if payload["name"] == "queue")
    assert {option["name"] for option in queue_payload["options"]} == {"status", "kick", "clear"}


def test_bot_uses_only_unprivileged_intents() -> None:
    bot = DraftBot()
    assert bot.intents.value == 641  # Guilds, voice states and guild messages.
    assert not bot.intents.members
    assert not bot.intents.message_content


def test_channel_overwrites_only_grant_what_the_bot_can_grant() -> None:
    # Discord rejects a whole channel create (50013) if an overwrite allows or denies anything the
    # bot lacks, and it refuses manage_permissions on overwrites for non-administrators.
    from bot.effects.gateway import HIDDEN, SELF_TEXT, SELF_VOICE, TEXT_MEMBER, VOICE_MEMBER

    invite = discord.Permissions(281475298741265)
    for overwrite in (HIDDEN, SELF_TEXT, SELF_VOICE, TEXT_MEMBER, VOICE_MEMBER):
        allow, deny = overwrite.pair()
        assert not allow.manage_permissions
        assert allow.is_subset(invite)
        assert deny.is_subset(invite)


def test_invite_permission_integer_is_what_the_docs_list() -> None:
    needed = discord.Permissions(
        create_instant_invite=True,
        manage_channels=True,
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
        read_message_history=True,
        connect=True,
        speak=True,
        use_voice_activation=True,
        move_members=True,
        manage_roles=True,
        set_voice_channel_status=True,
    )
    assert needed.value == 281475298741265


# --- Subs ------------------------------------------------------------------------------------


def _sent_custom_ids(interaction: MagicMock) -> list[str]:
    found = []
    for call in interaction.followup.send.call_args_list:
        view = call.kwargs.get("view")
        if view is not None:
            found.extend(
                str(child.custom_id)
                for child in view.walk_children()
                if hasattr(child, "custom_id")
            )
    return found


def test_sub_buttons_full_journey(wired: Community) -> None:
    from league.models import SubOffer, SubRequest
    from league.models.subs import SubStatus

    match, players = lobby_of_twelve(wired)
    leaving = players[3]
    sub = make_player(77)

    opener = fake_interaction(leaving)
    click(opener, "sub_open", match.pk)
    assert f"dl:v1:sub_out:{match.pk}:{leaving.pk}" in _sent_custom_ids(opener)

    click(fake_interaction(leaving), "sub_out", match.pk, str(leaving.pk))
    request = SubRequest.objects.get()
    assert request.status == SubStatus.OPEN

    picker = fake_interaction(leaving)
    click(picker, "sub_invite_open", match.pk, str(request.pk))
    assert f"dl:v1u:sub_invite:{match.pk}:{request.pk}" in _sent_custom_ids(picker)

    chooser = fake_interaction(leaving)
    chooser.data = {
        "values": [str(sub.discord_user_id)],
        "resolved": {"users": {str(sub.discord_user_id): {"username": "subby", "bot": False}}},
    }
    click(chooser, "sub_invite", match.pk, str(request.pk), (str(sub.discord_user_id),))
    assert "Asked" in replies(chooser)[0]
    offer = SubOffer.objects.get()

    answer = fake_interaction(sub, guild=False)
    answer.edit_original_response = AsyncMock()
    click(answer, "sub_accept", match.pk, str(offer.pk))
    assert "You're in" in replies(answer)[0]
    answer.edit_original_response.assert_awaited_once()
    assert fresh(match).participants.filter(player=sub, is_active=True).exists()


def test_a_bot_cannot_be_named_as_a_sub(wired: Community) -> None:
    from league.models import SubRequest

    match, players = lobby_of_twelve(wired)
    click(fake_interaction(players[3]), "sub_out", match.pk, str(players[3].pk))
    request = SubRequest.objects.get()
    chooser = fake_interaction(players[3])
    chooser.data = {"values": ["4242"], "resolved": {"users": {"4242": {"bot": True}}}}
    click(chooser, "sub_invite", match.pk, str(request.pk), ("4242",))
    assert "bot" in replies(chooser)[0]


def test_paused_ready_click_gets_a_clear_reply(wired: Community) -> None:
    match, players = lobby_of_twelve(wired)
    click(fake_interaction(players[3]), "sub_out", match.pk, str(players[3].pk))
    ready = fake_interaction(players[0])
    click(ready, "ready", match.pk)
    assert "paused" in replies(ready)[0]
