"""Bebot's community commands: the voice priority list, named pings and the error channel.
Admin commands default to administrators, and staff can use them too."""

from __future__ import annotations

import re
from typing import Any

import discord
from discord import Interaction, app_commands

from bot.db import db
from bot.error_log import discord_log_handler
from bot.handlers.common import require_staff
from bot.renderers.prio import render_prio
from bot.routing import guarded, reply
from league.domain.errors import InvalidAction
from league.models import Community
from league.services import community as community_service
from league.services import pings, prio


async def _community(interaction: Interaction[Any]) -> Community:
    guild = interaction.guild
    if guild is None:
        raise InvalidAction("Run this in your server.")
    return await db(community_service.ensure, guild.id, guild.name)


async def _staff(interaction: Interaction[Any]) -> Community:
    community = await _community(interaction)
    await require_staff(interaction, community)
    return community


def _channel_id(interaction: Interaction[Any]) -> int:
    if interaction.channel_id is None:
        raise InvalidAction("Run this in a server channel.")
    return interaction.channel_id


admin = app_commands.default_permissions(administrator=True)


# --- Priority list --------------------------------------------------------------------------


@app_commands.command(
    name="priolist", description="Create the permanent priority list display (Admin only)"
)
@admin
async def priolist_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        community = await _staff(interaction)
        await db(prio.set_display, community.pk, _channel_id(interaction))
        if not community.waiting_room_channel_id:
            await reply(
                interaction,
                "Priority list display created! It follows the Waiting Room, "
                "which isn't set up yet: run /setup first.",
            )
            return
        await reply(interaction, "Priority list display created!")

    await guarded(interaction, run)


@app_commands.command(name="prio", description="Show current draft queue (compact view)")
async def prio_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        community = await _community(interaction)
        view = render_prio(await db(prio.prio_view, community.pk))
        await interaction.response.send_message(view=view)

    await guarded(interaction, run)


@app_commands.command(name="refresh", description="Refresh the priority list display")
async def refresh_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        community = await _community(interaction)
        if not community.prio_channel_id:
            await reply(
                interaction, "No priority list display has been set up. Use /priolist first."
            )
            return
        await db(prio.request_render, community.pk)
        await reply(interaction, "Priority list has been refreshed!")

    await guarded(interaction, run)


@app_commands.command(name="removeprio", description="Remove a user from the priority list")
@app_commands.describe(user="The user to remove from priority")
@admin
async def removeprio_command(interaction: Interaction[Any], user: discord.Member) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        community = await _staff(interaction)
        if await db(prio.remove, community.pk, user.id):
            await reply(interaction, f"Removed {user.display_name} from priority list.")
        else:
            await reply(interaction, f"{user.display_name} is not in the priority list.")

    await guarded(interaction, run)


# --- Pings ----------------------------------------------------------------------------------

_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_USER_MENTION = re.compile(r"<@!?(\d+)>")


@app_commands.command(
    name="pingchannel",
    description=(
        "Add or replace a named ping in a channel (mention, cooldown, optional required role)"
    ),
)
@app_commands.describe(
    channel="The channel where ping is allowed",
    name="Name for this ping (used with /ping), e.g. rank or pugs",
    mention="Who to mention (e.g. @PUGS or role mention <@&role_id>)",
    timer="Cooldown between pings in minutes",
    required_role="Only members with this role can use this ping (leave empty for everyone)",
)
@admin
async def pingchannel_command(
    interaction: Interaction[Any],
    channel: discord.TextChannel,
    name: str,
    mention: str,
    timer: int,
    required_role: discord.Role | None = None,
) -> None:
    async def run() -> None:
        community = await _staff(interaction)
        ping = await db(
            pings.add,
            community.pk,
            channel.id,
            name,
            mention,
            timer,
            required_role.id if required_role else None,
        )
        role_note = f", restricted to {required_role.mention}" if required_role else ""
        await reply(
            interaction,
            f"Added ping `{ping.name}` in {channel.mention} "
            f"with {timer} minute cooldown{role_note}.",
        )

    await guarded(interaction, run)


@app_commands.command(name="removeping", description="Remove a named ping from a channel")
@app_commands.describe(channel="The channel the ping is in", name="Name of the ping to remove")
@admin
async def removeping_command(
    interaction: Interaction[Any], channel: discord.TextChannel, name: str
) -> None:
    async def run() -> None:
        await _staff(interaction)
        if await db(pings.remove, channel.id, name):
            await reply(interaction, f"Removed ping `{name}` from {channel.mention}.")
        else:
            await reply(interaction, f"No ping named `{name}` in {channel.mention}.")

    await guarded(interaction, run)


@app_commands.command(name="pings", description="List the pings set up in this channel")
async def pings_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        rows = await db(pings.in_channel, _channel_id(interaction))
        if not rows:
            await reply(interaction, "No pings are set up in this channel.")
            return
        lines = []
        for row in rows:
            remaining = pings.remaining_minutes(row)
            status = f"ready in {remaining:.0f}m" if remaining > 0 else "ready"
            role_note = f", needs <@&{row.required_role_id}>" if row.required_role_id else ""
            lines.append(
                f"`{row.name}` → {row.mention} "
                f"({row.timer_minutes}m cooldown{role_note}) - {status}"
            )
        await reply(interaction, "\n".join(lines)[:1900])

    await guarded(interaction, run)


async def _ping_names(
    interaction: Interaction[Any], current: str
) -> list[app_commands.Choice[str]]:
    if interaction.channel_id is None:
        return []
    rows = await db(pings.in_channel, interaction.channel_id)
    return [
        app_commands.Choice(name=row.name, value=row.name)
        for row in rows
        if current.lower() in row.name
    ][:25]


@app_commands.command(
    name="ping",
    description="Send a ping in this channel (mention + your message, respects cooldown)",
)
@app_commands.describe(
    message="Message to send after the mention",
    name="Which ping to use (optional if the channel only has one)",
)
@app_commands.autocomplete(name=_ping_names)
async def ping_command(
    interaction: Interaction[Any], message: str, name: str | None = None
) -> None:
    async def run() -> None:
        roles = getattr(interaction.user, "roles", [])
        ping = await db(pings.claim, _channel_id(interaction), name, {role.id for role in roles})
        content = f"{ping.mention} {message}".strip()[:2000]
        # Only what the admin configured can ping: never roles or users typed into the message.
        role_ids = [int(found) for found in _ROLE_MENTION.findall(ping.mention)]
        user_ids = [int(found) for found in _USER_MENTION.findall(ping.mention)]
        allowed = discord.AllowedMentions(
            everyone=False,
            roles=[discord.Object(id=role_id) for role_id in role_ids],
            users=[discord.Object(id=user_id) for user_id in user_ids],
        )
        await interaction.response.send_message(content, allowed_mentions=allowed)

    await guarded(interaction, run)


# --- Error channel --------------------------------------------------------------------------


@app_commands.command(
    name="seterrorchannel", description="Route bot error logs to a Discord channel in this guild"
)
@app_commands.describe(channel="Channel to post bot errors to")
@admin
async def seterrorchannel_command(
    interaction: Interaction[Any], channel: discord.TextChannel
) -> None:
    async def run() -> None:
        community = await _staff(interaction)
        discord_log_handler.remove_channel(community.error_channel_id)
        await db(pings.set_error_channel, community.pk, channel.id)
        discord_log_handler.add_channel(channel.id)
        await reply(interaction, f"Bot errors will now be posted to {channel.mention}.")

    await guarded(interaction, run)


@app_commands.command(
    name="clearerrorchannel", description="Stop posting bot errors to a channel in this guild"
)
@admin
async def clearerrorchannel_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        community = await _staff(interaction)
        discord_log_handler.remove_channel(community.error_channel_id)
        await db(pings.set_error_channel, community.pk, None)
        await reply(interaction, "Bot error channel cleared for this guild.")

    await guarded(interaction, run)


COMMANDS: tuple[app_commands.Command[Any, Any, Any], ...] = (
    priolist_command,
    prio_command,
    refresh_command,
    removeprio_command,
    pingchannel_command,
    removeping_command,
    pings_command,
    ping_command,
    seterrorchannel_command,
    clearerrorchannel_command,
)
