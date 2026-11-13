"""Bebot's standalone commands: /startdraft, /stopdraft, /coinflip, /captains_coinflip and
/randomdraft.

These work in any channel and don't need a match. The draft message is the command's own reply,
as it was in Bebot; later changes edit it through the outbox.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import Interaction, app_commands

from bot.db import db
from bot.handlers.common import is_staff
from bot.renderers.quickdraft import render_coinflip, render_quick_draft, render_random_draft
from bot.routing import guarded, reply
from league.domain.errors import InvalidAction
from league.models import Community
from league.services import community as community_service
from league.services import quickdraft

logger = logging.getLogger(__name__)


async def _community(interaction: Interaction[Any]) -> Community:
    guild = interaction.guild
    if guild is None:
        raise InvalidAction("Run this in a server channel.")
    return await db(community_service.ensure, guild.id, guild.name)


def _channel_id(interaction: Interaction[Any]) -> int:
    if interaction.channel_id is None:
        raise InvalidAction("Run this in a server channel.")
    return interaction.channel_id


async def _drop_placeholder(interaction: Interaction[Any]) -> None:
    """Take down the public "thinking..." reply so only the ephemeral error is left."""
    with contextlib.suppress(discord.HTTPException):
        await interaction.delete_original_response()


async def _open_draft(
    interaction: Interaction[Any], create: Callable[[Community], Awaitable[int]]
) -> None:
    """Acknowledge first, then create the draft and post it as the command's reply.

    Discord gives a command three seconds to answer, so the database work waits until it has
    been acknowledged. Every failure after the draft exists closes it again, so the channel is
    never left blocked by a draft nobody can see. If even that fails, `quickdraft.tick` closes it
    a minute later.
    """
    await interaction.response.defer(thinking=True)
    try:
        draft_id = await create(await _community(interaction))
    except Exception:
        await _drop_placeholder(interaction)
        raise
    try:
        state = await db(quickdraft.state, draft_id)
        message = await interaction.edit_original_response(view=render_quick_draft(state))
    except Exception:
        logger.warning("Couldn't post a new draft; closing it", extra={"draft": draft_id})
        with contextlib.suppress(Exception):
            await db(quickdraft.message_gone, draft_id)
        await _drop_placeholder(interaction)
        raise
    try:
        attached = await db(quickdraft.attach_message, draft_id, message.id)
    except Exception:
        attached = False
        logger.exception(
            "Couldn't save a new draft's message; closing it", extra={"draft": draft_id}
        )
        with contextlib.suppress(Exception):
            await db(quickdraft.message_gone, draft_id)
    if not attached:
        # Without its message ID nothing can update the draft, so take it down and let them retry.
        await _drop_placeholder(interaction)
        raise InvalidAction("The draft couldn't be set up. Please run the command again.")


@app_commands.command(name="startdraft", description="Start a new draft in this channel")
async def startdraft_command(interaction: Interaction[Any]) -> None:
    async def create(community: Community) -> int:
        draft = await db(
            quickdraft.start, community.pk, _channel_id(interaction), interaction.user.id
        )
        return draft.pk

    async def run() -> None:
        await _open_draft(interaction, create)

    await guarded(interaction, run)


@app_commands.command(name="stopdraft", description="Stop the current draft in this channel")
async def stopdraft_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        community = await _community(interaction)
        staff = await is_staff(interaction, community)
        await db(quickdraft.stop, _channel_id(interaction), interaction.user.id, is_staff=staff)
        await reply(interaction, "Draft stopped successfully.")

    await guarded(interaction, run)


@app_commands.command(
    name="coinflip",
    description="Flip a coin to decide which player gets The Hidden King (first pick)",
)
async def coinflip_command(
    interaction: Interaction[Any], player1: discord.Member, player2: discord.Member
) -> None:
    async def run() -> None:
        if player1.id == player2.id:
            await reply(interaction, "You can't flip a coin between the same player!")
            return
        first = (player1.id, player1.display_name)
        second = (player2.id, player2.display_name)
        winner, loser = (first, second) if secrets.randbelow(2) == 0 else (second, first)
        await interaction.response.send_message(view=render_coinflip(winner, loser))

    await guarded(interaction, run)


@app_commands.command(
    name="captains_coinflip",
    description="Coinflip between 2 captains to see who gets first choice",
)
async def captains_coinflip_command(
    interaction: Interaction[Any], captain1: discord.Member, captain2: discord.Member
) -> None:
    async def create(community: Community) -> int:
        draft = await db(
            quickdraft.start_coinflip,
            community.pk,
            _channel_id(interaction),
            interaction.user.id,
            (captain1.id, captain1.display_name),
            (captain2.id, captain2.display_name),
        )
        return draft.pk

    async def run() -> None:
        await _open_draft(interaction, create)

    await guarded(interaction, run)


@app_commands.command(
    name="randomdraft", description="Generate a draft of random heroes for both teams"
)
async def randomdraft_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await interaction.response.defer(thinking=True)
        try:
            community = await _community(interaction)
            draft = await db(quickdraft.random_draft, community)
            await interaction.edit_original_response(view=render_random_draft(draft))
        except Exception:
            await _drop_placeholder(interaction)
            raise

    await guarded(interaction, run)


COMMANDS: tuple[app_commands.Command[Any, Any, Any], ...] = (
    startdraft_command,
    stopdraft_command,
    coinflip_command,
    captains_coinflip_command,
    randomdraft_command,
)
