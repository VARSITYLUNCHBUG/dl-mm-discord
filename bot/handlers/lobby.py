from __future__ import annotations

from bot.db import db
from bot.handlers.common import actor
from bot.routing import Context, handles, reply
from league.services import lobby


@handles("ready")
async def ready(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(lobby.mark_ready, ctx.match_id, await actor(ctx.interaction))
    if ctx.interaction.guild_id is None:
        await reply(ctx.interaction, "You're ready. See you in the lobby.")  # No board in a DM.


@handles("captain_vote")
async def captain_vote(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    chosen = await db(
        lobby.cast_captain_votes,
        ctx.match_id,
        await actor(ctx.interaction),
        [int(value) for value in ctx.values],
    )
    names = " and ".join(f"**{player.display_name}**" for player in chosen)
    await reply(ctx.interaction, f"You voted for {names}. Vote again to change it.")
