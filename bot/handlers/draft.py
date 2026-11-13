from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import Interaction

from bot.db import db
from bot.handlers.common import actor
from bot.routing import Context, guarded, handles, reply
from league.integrations import deadlock_api
from league.models import Player
from league.services import draft


@handles("pick_player")
async def pick_player(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    step_index, target = ctx.arg.split(".")  # "<step>.<player>"
    await db(
        draft.pick_player,
        ctx.match_id,
        await actor(ctx.interaction),
        int(step_index),
        int(target),
        ctx.interaction.id,
    )


async def _choose_champion(ctx: Context, step_index: int, champion_id: int) -> None:
    await db(
        draft.choose_champion,
        ctx.match_id,
        await actor(ctx.interaction),
        step_index,
        champion_id,
        ctx.interaction.id,
    )


@handles("choose_champion")
async def choose_champion(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    step_index, champion_id = ctx.arg.split(".")  # "<step>.<champion>"
    await _choose_champion(ctx, int(step_index), int(champion_id))


@handles("pick_champion")
async def pick_champion(ctx: Context) -> None:
    """The dropdown fallback for a roster too big for the grid. The arg is "<step>.<select>"."""
    await ctx.interaction.response.defer()
    await _choose_champion(ctx, int(ctx.arg.split(".")[0]), int(ctx.values[0]))


@handles("random_champion")
async def random_champion(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(
        draft.choose_random_champion,
        ctx.match_id,
        await actor(ctx.interaction),
        int(ctx.arg),
        ctx.interaction.id,
    )


@handles("claim_side")
async def claim_side(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(draft.claim_side, ctx.match_id, await actor(ctx.interaction), ctx.arg)


class LobbyCodeModal(discord.ui.Modal, title="Enter the lobby code"):
    code: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="Custom lobby code",
        min_length=3,
        max_length=32,
        placeholder="From the Deadlock lobby",
    )

    def __init__(self, match_id: int, player: Player) -> None:
        super().__init__(timeout=600)
        self.match_id = match_id
        self.player = player

    async def on_submit(self, interaction: Interaction[Any]) -> None:
        async def submit() -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await db(draft.submit_lobby_code, self.match_id, self.player, str(self.code.value))
            await reply(interaction, "Code shared with both teams.")

        await guarded(interaction, submit)


@handles("code_open")
async def code_open(ctx: Context) -> None:
    # A modal must be the first response, so this can't defer: the check has to stay quick.
    player = await actor(ctx.interaction)
    await db(draft.check_lobby_host, ctx.match_id, player)
    await ctx.interaction.response.send_modal(LobbyCodeModal(ctx.match_id, player))


@handles("api_start")
async def api_start(ctx: Context) -> None:
    """A captain starts the bot-hosted lobby once everyone is seated."""
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    player = await actor(ctx.interaction)
    party_id = await db(draft.check_can_start_api, ctx.match_id, player)
    try:
        await asyncio.to_thread(deadlock_api.start_custom_match, party_id)
    except deadlock_api.ApiError as exc:
        hint = "Try again in a moment." if exc.rate_limited else "Start it from the game instead."
        await reply(ctx.interaction, f"deadlock-api couldn't start the match. {hint}")
        return
    await db(draft.mark_api_started, ctx.match_id, player)
    await reply(ctx.interaction, "Started.")
