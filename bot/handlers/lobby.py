from __future__ import annotations

from typing import Any

from discord.ui import ActionRow, Container, LayoutView, TextDisplay

from bot.db import db
from bot.handlers.common import actor
from bot.renderers.board import BUTTONS_PER_ROW
from bot.routing import Context, RouteButton, handles, reply
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


@handles("keep")
async def keep(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(lobby.keep_captaincy, ctx.match_id, await actor(ctx.interaction))


@handles("handoff_open")
async def handoff_open(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    candidates = await db(lobby.handoff_candidates, ctx.match_id, await actor(ctx.interaction))
    box: Container[Any] = Container(
        TextDisplay("Who should take over? They have to accept, and a decline uses up a request.")
    )
    for start in range(0, len(candidates), BUTTONS_PER_ROW):
        box.add_item(
            ActionRow(
                *[
                    RouteButton(
                        "handoff_select", ctx.match_id, player.pk, label=player.display_name[:80]
                    )
                    for player in candidates[start : start + BUTTONS_PER_ROW]
                ]
            )
        )
    view = LayoutView(timeout=None)
    view.add_item(box)
    await ctx.interaction.followup.send(view=view, ephemeral=True)


@handles("handoff_select")
async def handoff_select(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    await db(lobby.request_handoff, ctx.match_id, await actor(ctx.interaction), int(ctx.arg))
    await reply(ctx.interaction, "Request sent. They have a short time to accept.")


async def _respond(ctx: Context, accept: bool) -> None:
    await ctx.interaction.response.defer()
    await db(lobby.respond_handoff, int(ctx.arg), await actor(ctx.interaction), accept)


@handles("handoff_accept")
async def handoff_accept(ctx: Context) -> None:
    await _respond(ctx, True)


@handles("handoff_decline")
async def handoff_decline(ctx: Context) -> None:
    await _respond(ctx, False)
