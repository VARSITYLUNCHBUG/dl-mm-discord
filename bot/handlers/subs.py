"""Sub out and sub in. Everything is checked again under the match lock, so these stay thin."""

from __future__ import annotations

import contextlib
from typing import Any

import discord
from discord import ButtonStyle
from discord.ui import ActionRow, Container, LayoutView, TextDisplay

from bot.db import db
from bot.handlers.common import actor
from bot.renderers.board import BUTTONS_PER_ROW, render_offer_answered
from bot.routing import Context, RouteButton, RouteUserSelect, handles, picked_users, reply
from league.domain.errors import InvalidAction
from league.models.matches import ResourceKind
from league.services import effects, subs


def _channel_mention(match_id: int) -> str:
    channel_id = effects.resource_id(match_id, ResourceKind.TEXT)
    return f"<#{channel_id}>" if channel_id else f"match #{match_id}'s channel"


# --- Sub out ---------------------------------------------------------------------------------


@handles("sub_open")
async def sub_open(ctx: Context) -> None:
    """Show who this player can ask to sub out: themself, and their teammates if they captain."""
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    options = await db(subs.sub_out_options, ctx.match_id, await actor(ctx.interaction))
    lines = ["Who needs to come out? The match pauses until a sub takes the spot."]
    if any(not choice.is_self for choice in options.choices):
        lines.append("Subbing out a teammate needs the other captain to agree.")
    if options.game_on:
        lines.append("**The game is on:** leaving now gives you a queue cooldown.")
    box: Container[Any] = Container(TextDisplay("\n".join(lines)))
    buttons = [
        RouteButton(
            "sub_out",
            ctx.match_id,
            choice.player_id,
            label="Sub me out" if choice.is_self else choice.name[:80],
            style=ButtonStyle.danger if choice.is_self else ButtonStyle.secondary,
        )
        for choice in options.choices
    ]
    for start in range(0, len(buttons), BUTTONS_PER_ROW):
        box.add_item(ActionRow(*buttons[start : start + BUTTONS_PER_ROW]))
    view = LayoutView(timeout=None)
    view.add_item(box)
    await ctx.interaction.followup.send(view=view, ephemeral=True)


@handles("sub_out")
async def sub_out(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    player = await actor(ctx.interaction)
    request = await db(subs.request_sub_out, ctx.match_id, player, int(ctx.arg))
    if request.is_self:
        await reply(
            ctx.interaction,
            "You're subbing out. Name someone on the board, or the queue gets asked.",
        )
    else:
        await reply(ctx.interaction, "Asked the other captain to approve.")


async def _answer_sub_out(ctx: Context, approve: bool) -> None:
    await ctx.interaction.response.defer()
    await db(subs.answer_sub_out, int(ctx.arg), await actor(ctx.interaction), approve)


@handles("sub_approve")
async def sub_approve(ctx: Context) -> None:
    await _answer_sub_out(ctx, True)


@handles("sub_reject")
async def sub_reject(ctx: Context) -> None:
    await _answer_sub_out(ctx, False)


@handles("sub_cancel")
async def sub_cancel(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(subs.cancel_sub, int(ctx.arg), await actor(ctx.interaction))


# --- Sub in ----------------------------------------------------------------------------------


@handles("sub_invite_open")
async def sub_invite_open(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    await db(subs.check_can_invite, int(ctx.arg), await actor(ctx.interaction))
    box: Container[Any] = Container(
        TextDisplay("Who should sub in? They get a DM and have to accept."),
        ActionRow(
            RouteUserSelect("sub_invite", ctx.match_id, ctx.arg, placeholder="Choose a player")
        ),
    )
    view = LayoutView(timeout=None)
    view.add_item(box)
    await ctx.interaction.followup.send(view=view, ephemeral=True)


@handles("sub_invite")
async def sub_invite(ctx: Context) -> None:
    await ctx.interaction.response.defer(ephemeral=True, thinking=True)
    picked = picked_users(ctx.interaction)
    if not picked:
        raise InvalidAction("Choose someone to ask.")
    target = picked[0]
    if target.is_bot:
        raise InvalidAction("A bot can't sub in.")
    offer = await db(
        subs.invite_sub, int(ctx.arg), await actor(ctx.interaction), target.id, target.name
    )
    stamp = int(offer.deadline_at.timestamp())
    await reply(ctx.interaction, f"Asked {target.name}. They have until <t:{stamp}:T> to accept.")


@handles("sub_queue")
async def sub_queue(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(subs.offer_to_queue, int(ctx.arg), await actor(ctx.interaction))


async def _close_dm(ctx: Context, text: str) -> None:
    """Swap the offer for its answer, so its buttons can't be pressed again. Cosmetic only."""
    with contextlib.suppress(discord.HTTPException):
        await ctx.interaction.edit_original_response(view=render_offer_answered(ctx.match_id, text))


@handles("sub_accept")
async def sub_accept(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(subs.answer_offer, int(ctx.arg), await actor(ctx.interaction), True)
    channel = await db(_channel_mention, ctx.match_id)
    await _close_dm(ctx, f"✅ You're in. Head to {channel}.")
    await reply(ctx.interaction, f"You're in. Head to {channel}: the bot is moving you to voice.")


@handles("sub_decline")
async def sub_decline(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(subs.answer_offer, int(ctx.arg), await actor(ctx.interaction), False)
    await _close_dm(ctx, "No problem: the spot goes to someone else.")
