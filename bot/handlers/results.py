from __future__ import annotations

import io
import logging
from typing import Any

import discord
from discord import Interaction, SelectOption

from bot.db import db
from bot.handlers.common import actor, require_staff_for_match
from bot.images import MAX_IMAGE_BYTES, detect_image, sha256
from bot.routing import Context, RouteSelect, guarded, handles, reply
from league.domain.errors import InvalidAction, NotAllowed
from league.models import Match, Player
from league.services import awards, early_leave, ocr, results
from league.services.boards import team_name
from league.services.common import captain_of, roster
from league.services.results import ImageRef

logger = logging.getLogger(__name__)


def _archive_channel_id(match_id: int) -> int | None:
    return Match.objects.select_related("community").get(pk=match_id).community.archive_channel_id


def _captain_names(match_id: int) -> dict[str, str]:
    match = Match.objects.get(pk=match_id)
    return {team: captain_of(match, team).player.display_name for team in ("A", "B")}


def _roster_options(match_id: int) -> list[tuple[int, str, str]]:
    """(player ID, name, team) for everyone on the roster, for the award pickers."""
    match = Match.objects.get(pk=match_id)
    rows = roster(match).select_related("player").order_by("team", "player__display_name")
    return [(row.player_id, row.player.display_name, row.team) for row in rows]


def parse_deadlock_match_id(raw: str) -> int | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    if not cleaned.isdigit() or len(cleaned) > 18:
        raise InvalidAction(
            "The Deadlock match ID should be numbers only. Leave it blank if unsure."
        )
    return int(cleaned)


async def archive_screenshot(
    interaction: Interaction[Any], match_id: int, player: Player, attachment: discord.Attachment
) -> ImageRef:
    """Copy the upload to the archive straight away: Discord's CDN URLs expire, IDs don't."""
    if attachment.size > MAX_IMAGE_BYTES:
        raise InvalidAction("That file is too large: the limit is 20 MB.")
    data = await attachment.read()
    extension = detect_image(data)
    if extension is None:
        raise InvalidAction("That file isn't a PNG, JPEG or WebP image.")
    channel_id = await db(_archive_channel_id, match_id)
    if channel_id is None:
        raise InvalidAction("The archive channel isn't set up. Ask staff to run /setup.")
    channel = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(
        channel_id
    )
    filename = f"match-{match_id}-player-{player.pk}.{extension}"
    message = await channel.send(  # type: ignore[union-attr]
        f"Match #{match_id}: submitted by {player.display_name}",
        file=discord.File(io.BytesIO(data), filename=filename),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return ImageRef(
        sha256=sha256(data),
        archive_channel_id=channel_id,
        archive_message_id=message.id,
        attachment_id=message.attachments[0].id,
    )


async def process_report(
    interaction: Interaction[Any],
    match_id: int,
    player: Player,
    winner: str,
    attachment: discord.Attachment,
    deadlock_match_id: str,
    picked: dict[int, str] | None = None,
) -> None:
    """Shared by the modal and /submit. The interaction must already be deferred."""
    parsed_id = parse_deadlock_match_id(deadlock_match_id)
    image = await archive_screenshot(interaction, match_id, player, attachment)
    status = await db(
        results.submit_report, match_id, player, winner, image, parsed_id, picked or None
    )
    messages = {
        "awaiting_confirmation": "Thanks: the other captain can now confirm or dispute.",
        "completed": "Thanks: both captains agree, and the result is posted.",
        "disputed": "You and the other captain disagree, so staff will decide.",
    }
    await reply(interaction, messages.get(status, "Thanks: result received."))


class ReportModal(discord.ui.Modal):
    """Asks who won only if the button didn't already say, as "Add my screenshot" doesn't."""

    def __init__(
        self,
        match_id: int,
        player: Player,
        captains: dict[str, str],
        winner: str | None = None,
        players: list[tuple[int, str, str]] | None = None,
    ) -> None:
        # The title repeats the choice, so a mistaken click is easy to spot and cancel.
        title = f"{team_name(captains[winner])} won" if winner else "Report the result"
        super().__init__(title=title[:45], timeout=900)
        self.match_id = match_id
        self.player = player
        self.winner = winner
        self.winner_select: discord.ui.Select[Any] | None = None
        if winner is None:
            self.winner_select = discord.ui.Select(
                options=[
                    SelectOption(label=team_name(name)[:100], value=team)
                    for team, name in captains.items()
                ],
                placeholder="Who won?",
            )
            self.add_item(discord.ui.Label(text="Winning team", component=self.winner_select))
        self.screenshot: discord.ui.FileUpload[Any] = discord.ui.FileUpload(
            required=True, min_values=1, max_values=1
        )
        self.deadlock_id: discord.ui.TextInput[Any] = discord.ui.TextInput(
            required=False, max_length=18, placeholder="Optional"
        )
        self.add_item(discord.ui.Label(text="Scoreboard screenshot", component=self.screenshot))
        self.add_item(discord.ui.Label(text="Deadlock match ID", component=self.deadlock_id))
        # Optional: a scoreboard read replaces these when it is confident.
        self.mvp: discord.ui.Select[Any] | None = None
        self.keys: discord.ui.Select[Any] | None = None
        options = [
            SelectOption(label=f"{name} ({team_name(captains[team])})"[:100], value=str(pid))
            for pid, name, team in (players or [])[:25]
            if team in captains
        ]
        if options:
            self.mvp = discord.ui.Select(
                options=options, placeholder="Optional", min_values=0, max_values=1, required=False
            )
            self.keys = discord.ui.Select(
                options=options,
                placeholder="Optional, up to 2",
                min_values=0,
                max_values=min(2, len(options)),
                required=False,
            )
            self.add_item(discord.ui.Label(text="MVP", component=self.mvp))
            self.add_item(discord.ui.Label(text="Key players", component=self.keys))

    def picked(self) -> dict[int, str]:
        mvp = int(self.mvp.values[0]) if self.mvp and self.mvp.values else None
        keys = [int(value) for value in self.keys.values] if self.keys else []
        return awards.from_picks(mvp, [key for key in keys if key != mvp])

    async def on_submit(self, interaction: Interaction[Any]) -> None:
        async def submit() -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await process_report(
                interaction,
                self.match_id,
                self.player,
                self.winner or (self.winner_select.values[0] if self.winner_select else ""),
                self.screenshot.values[0],
                str(self.deadlock_id.value or ""),
                self.picked(),
            )

        await guarded(interaction, submit)


@handles("report_open")
async def report_open(ctx: Context) -> None:
    # A modal must be the first response, so this can't defer: the checks have to stay quick.
    player = await actor(ctx.interaction)
    await db(results.check_can_report, ctx.match_id, player)
    captains = await db(_captain_names, ctx.match_id)
    players = await db(_roster_options, ctx.match_id)
    winner = ctx.arg if ctx.arg in captains else None
    await ctx.interaction.response.send_modal(
        ReportModal(ctx.match_id, player, captains, winner, players)
    )


@handles("confirm")
async def confirm(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(results.confirm, ctx.match_id, await actor(ctx.interaction))


class DisputeModal(discord.ui.Modal, title="Dispute the result"):
    note: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="What happened?", style=discord.TextStyle.paragraph, max_length=500
    )

    def __init__(self, match_id: int, player: Player) -> None:
        super().__init__(timeout=600)
        self.match_id = match_id
        self.player = player

    async def on_submit(self, interaction: Interaction[Any]) -> None:
        async def submit() -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await db(results.dispute, self.match_id, self.player, str(self.note.value))
            await reply(interaction, "Sent to staff. Ratings won't change until they decide.")

        await guarded(interaction, submit)


@handles("dispute_open")
async def dispute_open(ctx: Context) -> None:
    player = await actor(ctx.interaction)
    await db(results.check_can_report, ctx.match_id, player)
    await ctx.interaction.response.send_modal(DisputeModal(ctx.match_id, player))


async def _resolve(ctx: Context, outcome: str) -> None:
    await ctx.interaction.response.defer()
    await require_staff_for_match(ctx.interaction, ctx.match_id)
    player = await actor(ctx.interaction)
    await db(
        results.resolve_dispute, ctx.match_id, outcome, player, f"Decided by {player.display_name}"
    )


@handles("resolve_a")
async def resolve_a(ctx: Context) -> None:
    await _resolve(ctx, "A")


@handles("resolve_b")
async def resolve_b(ctx: Context) -> None:
    await _resolve(ctx, "B")


@handles("resolve_void")
async def resolve_void(ctx: Context) -> None:
    await _resolve(ctx, "void")


# --- Matching scoreboard names ---------------------------------------------------------------


async def _is_staff(interaction: Interaction[Any], match_id: int) -> bool:
    try:
        await require_staff_for_match(interaction, match_id)
    except NotAllowed:
        return False
    return True


def name_picker(match_id: int, read_id: int) -> discord.ui.LayoutView | None:
    """One select per unmatched scoreboard row, offering that team's unmatched players."""
    rows = ocr.unmapped_rows(read_id)
    if not rows:
        return None
    box: discord.ui.Container[Any] = discord.ui.Container(
        discord.ui.TextDisplay("Who was each of these on the scoreboard?")
    )
    for row in rows[:10]:
        options = [SelectOption(label=name[:100], value=str(pid)) for pid, name in row.options]
        if not options:
            continue
        hero = f" ({row.hero_name})" if row.hero_name else ""
        box.add_item(discord.ui.TextDisplay(f"**{row.ocr_name or '?'}**{hero}"))
        box.add_item(
            discord.ui.ActionRow(
                RouteSelect(
                    "ocr_pick",
                    match_id,
                    f"{read_id}.{row.row}",
                    options=options[:25],
                    placeholder=f"Who played {row.ocr_name or 'this row'}?"[:150],
                )
            )
        )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


async def send_name_picker(interaction: Interaction[Any], match_id: int, read_id: int) -> None:
    view = await db(name_picker, match_id, read_id)
    if view is None:
        await reply(interaction, "Every scoreboard name is matched.")
        return
    await interaction.followup.send(view=view, ephemeral=True)


@handles("ocr_map")
async def ocr_map(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    player = await actor(ctx.interaction)
    staff = await _is_staff(ctx.interaction, ctx.match_id)
    await db(ocr.can_map, int(ctx.arg), player, staff)
    await send_name_picker(ctx.interaction, ctx.match_id, int(ctx.arg))


@handles("ocr_pick")
async def ocr_pick(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    read_id, row = (int(part) for part in ctx.arg.split(".", 1))
    if not ctx.values:
        raise InvalidAction("Choose a player.")
    player = await actor(ctx.interaction)
    staff = await _is_staff(ctx.interaction, ctx.match_id)
    left = await db(ocr.map_row, read_id, row, int(ctx.values[0]), player, is_staff=staff)
    await reply(
        interaction=ctx.interaction,
        text="Matched. Every name is placed now." if left == 0 else f"Matched. {left} to go.",
    )


# --- Early leave -----------------------------------------------------------------------------


def _early_leave_picker(match_id: int, player: Player) -> discord.ui.LayoutView:
    match = Match.objects.get(pk=match_id)
    if not match.participants.filter(player=player, is_captain=True, is_active=True).exists():
        raise NotAllowed("Only a captain can report an early leave.")
    if not early_leave.can_report(match):
        raise InvalidAction("It's too late to report an early leave. Ask staff if it matters.")
    options = [
        SelectOption(
            label=f"{name} ({team_name(captain_of(match, team).player.display_name)})"[:100],
            value=str(pid),
        )
        for pid, name, team in _roster_options(match_id)
    ]
    box: discord.ui.Container[Any] = discord.ui.Container(
        discord.ui.TextDisplay(
            "Who left? The other captain has to agree, then the match is voided with no rating "
            "change and that player gets a leaver penalty."
        ),
        discord.ui.ActionRow(
            RouteSelect("early_pick", match_id, options=options[:25], placeholder="Who left?")
        ),
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(box)
    return view


@handles("early_open")
async def early_open(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    view = await db(_early_leave_picker, ctx.match_id, await actor(ctx.interaction))
    await ctx.interaction.followup.send(view=view, ephemeral=True)


@handles("early_pick")
async def early_pick(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    if not ctx.values:
        raise InvalidAction("Choose who left.")
    await db(early_leave.report, ctx.match_id, await actor(ctx.interaction), int(ctx.values[0]))
    await reply(ctx.interaction, "Sent. The other captain can agree on the board.")


@handles("early_yes")
async def early_yes(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(early_leave.answer, ctx.match_id, await actor(ctx.interaction), True)


@handles("early_no")
async def early_no(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    await db(early_leave.answer, ctx.match_id, await actor(ctx.interaction), False)
    await reply(ctx.interaction, "Noted: staff will decide.")
