"""The bot process: gateway connection, startup recovery and the two background loops."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

import discord
from discord import app_commands
from django.conf import settings

from bot.commands import player, setup, staff
from bot.db import db
from bot.effects.gateway import DiscordGateway
from bot.effects.worker import EffectWorker

# Importing the handlers registers them with the router.
from bot.handlers import draft as _draft  # noqa: F401
from bot.handlers import lobby as _lobby  # noqa: F401
from bot.handlers import queue as _queue  # noqa: F401
from bot.handlers import results as _results  # noqa: F401
from bot.handlers import subs as _subs  # noqa: F401
from bot.routing import RouteButton, RouteSelect, RouteUserSelect
from league.services import deadlines, effects

logger = logging.getLogger(__name__)

DEADLINE_INTERVAL_SECONDS = 1.0
EFFECT_IDLE_SECONDS = 0.5
PRUNE_INTERVAL_SECONDS = 24 * 3600


class DraftBot(discord.Client):
    def __init__(self) -> None:
        # The bot moves members but never joins voice itself, so it has no use for voice libraries.
        discord.VoiceClient.warn_nacl = False
        discord.VoiceClient.warn_dave = False
        # None is privileged. Guild messages only tell us that someone chatted in a match channel;
        # without the Message Content intent the bot can't read what they said.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,  # Nothing reads the message cache, so don't spend memory on it.
        )
        self.tree = app_commands.CommandTree(self)
        self.gateway = DiscordGateway(self)
        self.worker = EffectWorker(self.gateway, on_lane_done=self.wake_effects)
        self._effects_wake = asyncio.Event()
        self._started = False
        self._tasks: list[asyncio.Task[None]] = []

    def wake_effects(self) -> None:
        """Called after each click, so board updates follow immediately rather than on a poll."""
        self._effects_wake.set()

    async def setup_hook(self) -> None:
        self.add_dynamic_items(RouteButton, RouteSelect, RouteUserSelect)
        commands: tuple[Any, ...] = (
            *player.COMMANDS,
            setup.setup_command,
            *staff.GROUPS,
            *staff.COMMANDS,
        )
        for command in commands:
            self.tree.add_command(command)
        if settings.DISCORD_GUILD_ID:
            guild = discord.Object(id=settings.DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)  # Guild commands update instantly.
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        logger.info("Connected", extra={"user": str(self.user), "guilds": len(self.guilds)})
        if self._started:
            return  # on_ready fires again after reconnects.
        self._started = True
        extended = await db(deadlines.extend_past_due)
        logger.info("Startup grace applied", extra={"extended": extended})
        await db(effects.request_reconcile)
        await self._sweep_stale_channels()
        self._tasks = [
            asyncio.create_task(self._deadline_loop(), name="deadlines"),
            asyncio.create_task(self._effect_loop(), name="effects"),
        ]

    async def _sweep_stale_channels(self) -> None:
        """Delete channels whose cleanup failed for good. By stored ID only, never by name."""
        for match_id, kind, channel_id in await db(effects.stale_resources):
            try:
                await self.gateway.delete_channel(channel_id)
                await db(effects.mark_resource_deleted, match_id, kind)
            except discord.HTTPException:
                logger.exception("Sweep failed", extra={"match": match_id, "channel": channel_id})

    async def _deadline_loop(self) -> None:
        last_prune = time.monotonic()
        while True:
            try:
                fired = await db(deadlines.tick)
                if fired:
                    self.wake_effects()
                settings.BOT_HEARTBEAT_FILE.touch()
                if time.monotonic() - last_prune > PRUNE_INTERVAL_SECONDS:
                    last_prune = time.monotonic()
                    await db(effects.prune_finished)
            except Exception:
                logger.exception("Deadline loop error")
            await asyncio.sleep(DEADLINE_INTERVAL_SECONDS)

    async def _effect_loop(self) -> None:
        """Hand due effects to per-match lanes. Wakes on a click, a fired timer or a lane ending."""
        while True:
            try:
                self._effects_wake.clear()
                await self.worker.dispatch()
            except Exception:
                logger.exception("Effect loop error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._effects_wake.wait(), EFFECT_IDLE_SECONDS)

    async def on_message(self, message: discord.Message) -> None:
        """Chat is welcome in match channels, but the board has to stay in view."""
        if message.guild is None or self.user is None or message.author.id == self.user.id:
            return
        try:
            await db(effects.note_chat, message.channel.id)
        except Exception:
            logger.exception("Couldn't note chat", extra={"channel": message.channel.id})

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Joining the Waiting Room after your lobby popped moves you straight into it."""
        channel = after.channel
        if channel is None or before.channel == channel:
            return
        try:
            target = await db(effects.voice_target, member.id, channel.id)
            if target is not None:
                await self.gateway.move_member(target[0], member.id, target[1])
        except Exception:
            logger.exception("Auto-move failed", extra={"user": member.id})


def run() -> None:
    if not settings.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN isn't set. Add it to .env.")
    # In a container the bot is PID 1, which ignores SIGTERM by default, so `docker compose stop`
    # waited out the grace period and then killed it. Treat SIGTERM like Ctrl+C: discord.py then
    # closes the connection cleanly, and any effect cut short stays pending and runs on restart.
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    DraftBot().run(settings.DISCORD_TOKEN, log_handler=None)


def _stop_on_sigterm(signum: int, frame: object) -> None:
    logger.info("SIGTERM received, shutting down")
    raise KeyboardInterrupt
