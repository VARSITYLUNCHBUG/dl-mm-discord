from __future__ import annotations

from bot.db import db
from bot.handlers.common import actor, community_of
from bot.routing import Context, handles
from league.services import queue

# Success says nothing: the queue board updates in front of them. Refusals still get a reply.


@handles("join")
async def join(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    community = await community_of(ctx.interaction)
    player = await actor(ctx.interaction)
    await db(queue.join, community.pk, player)


@handles("leave")
async def leave(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    community = await community_of(ctx.interaction)
    player = await actor(ctx.interaction)
    await db(queue.leave, community.pk, player)
