"""Clicks on a standalone draft (/startdraft, /captains_coinflip). The match slot holds its ID."""

from __future__ import annotations

from django.conf import settings

from bot.db import db
from bot.routing import Context, handles, reply
from league.services import quickdraft


def _name(ctx: Context) -> str:
    user = ctx.interaction.user
    return getattr(user, "display_name", None) or user.name


@handles("qd_captain")
async def claim_captain(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    label = await db(
        quickdraft.claim_captain,
        ctx.match_id,
        ctx.arg,
        ctx.interaction.user.id,
        _name(ctx),
        allow_both=settings.DEBUG,  # Bebot let one person captain both sides when testing.
    )
    await reply(ctx.interaction, f"You are now captain of {label}!")


@handles("qd_coin")
async def coinflip_choice(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(quickdraft.coinflip_choose, ctx.match_id, ctx.interaction.user.id, ctx.arg)


@handles("qd_start")
async def start_draft(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(quickdraft.start_after_coinflip, ctx.match_id, ctx.interaction.user.id)


@handles("qd_hero")
async def choose_hero(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    step_index, champion_id = ctx.arg.split(".")  # "<step>.<champion>"
    await db(
        quickdraft.choose_hero,
        ctx.match_id,
        ctx.interaction.user.id,
        int(step_index),
        int(champion_id),
    )


@handles("qd_random")
async def random_hero(ctx: Context) -> None:
    await ctx.interaction.response.defer()
    await db(quickdraft.choose_random, ctx.match_id, ctx.interaction.user.id, int(ctx.arg))


@handles("qd_pick")
async def pick_hero(ctx: Context) -> None:
    """The dropdown fallback for a roster too big for the grid. The arg is "<step>.<select>"."""
    await ctx.interaction.response.defer()
    await db(
        quickdraft.choose_hero,
        ctx.match_id,
        ctx.interaction.user.id,
        int(ctx.arg.split(".")[0]),
        int(ctx.values[0]),
    )
