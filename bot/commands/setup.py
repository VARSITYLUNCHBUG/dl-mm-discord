"""/setup creates the permanent channels. Running it again only fills in whatever is missing."""

from __future__ import annotations

from typing import Any

import discord
from discord import Interaction, app_commands

from bot.db import db
from bot.effects.gateway import SELF_TEXT, SELF_VOICE
from bot.routing import guarded, reply
from league.models import Community
from league.services import common, community
from league.services import community as community_service


def _is_live(guild: discord.Guild, channel_id: int | None) -> bool:
    return channel_id is not None and guild.get_channel(channel_id) is not None


def _self(client: Any) -> discord.Object:
    return discord.Object(id=client.user.id, type=discord.Member)


async def ensure_channels(
    interaction: Interaction[Any], row: Community, staff_role: discord.Role | None
) -> None:
    guild = interaction.guild
    assert guild is not None
    me = _self(interaction.client)
    everyone = guild.default_role
    read_only = discord.PermissionOverwrite(view_channel=True, send_messages=False)
    wiring: dict[str, int | None] = {}
    if staff_role is not None:
        wiring["staff_role_id"] = staff_role.id

    category: Any = guild.get_channel(row.match_category_id or 0)
    if category is None:
        category = await guild.create_category(
            "Deadlock", overwrites={me: SELF_VOICE}, reason="Draft bot setup"
        )
        wiring["match_category_id"] = category.id

    if not _is_live(guild, row.waiting_room_channel_id):
        voice = await guild.create_voice_channel(
            "Waiting Room", category=category, overwrites={me: SELF_VOICE}
        )
        wiring["waiting_room_channel_id"] = voice.id
    if not _is_live(guild, row.queue_channel_id):
        channel = await guild.create_text_channel(
            "queue", category=category, overwrites={everyone: read_only, me: SELF_TEXT}
        )
        wiring["queue_channel_id"] = channel.id
        wiring["queue_message_id"] = None
    if not _is_live(guild, row.results_channel_id):
        channel = await guild.create_text_channel(
            "results", category=category, overwrites={everyone: read_only, me: SELF_TEXT}
        )
        wiring["results_channel_id"] = channel.id
    if not _is_live(guild, row.archive_channel_id):
        hidden = discord.PermissionOverwrite(view_channel=False)
        overwrites: dict[Any, discord.PermissionOverwrite] = {everyone: hidden, me: SELF_TEXT}
        if staff_role is not None:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False
            )
        channel = await guild.create_text_channel(
            "scoreboard-archive", category=category, overwrites=overwrites
        )
        wiring["archive_channel_id"] = channel.id

    if wiring:
        await db(community_service.save_wiring, row.pk, **wiring)


def _refresh_queue(community_id: int) -> None:
    common.render_queue(Community.objects.get(pk=community_id))


@app_commands.command(name="setup", description="Create the queue, voice and results channels")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(staff_role="The role allowed to use staff commands")
async def setup_command(
    interaction: Interaction[Any], staff_role: discord.Role | None = None
) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await reply(interaction, "Run this in your server.")
            return
        row = await db(community.ensure, guild.id, guild.name)
        await ensure_channels(interaction, row, staff_role)
        await db(_refresh_queue, row.pk)
        await reply(interaction, "Setup done. The queue is live.")

    await guarded(interaction, run)
