"""Runs outbox effects, in order per match, with retry and backoff.

Each match gets its own lane, so a slow Discord call in one match (deleting voice channels,
uploading a screenshot, waiting out a rate limit) never holds up another match's board.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable

import discord

from bot.db import db
from bot.effects.gateway import ChannelGone, Gateway
from bot.effects.handlers import HANDLERS, Env, Skip
from league.models import OutboxEffect
from league.models.outbox import EffectStatus
from league.services import effects, lobby

logger = logging.getLogger(__name__)

# Discord will keep refusing these whatever we do, so retrying only delays the fallback.
_PERMANENT_STATUS = {400, 401, 403, 404}


class EffectWorker:
    def __init__(self, gateway: Gateway, on_lane_done: Callable[[], None] | None = None) -> None:
        self.gateway = gateway
        # Called when a lane finishes, since its effects may have queued more work.
        self.on_lane_done = on_lane_done
        # One running task per match (the None key holds effects that belong to no match).
        self._lanes: dict[int | None, asyncio.Task[int]] = {}

    @property
    def busy_matches(self) -> set[int | None]:
        return set(self._lanes)

    async def dispatch(self) -> int:
        """Start a lane for each match that has due effects and no lane running. Doesn't wait.

        A match whose lane is still busy is left alone: its due effects are picked up once the
        lane finishes, which keeps that match's effects in order.
        """
        started = 0
        for group in await db(effects.due_by_match):
            key = group[0].match_id
            if key in self._lanes:
                continue
            task = asyncio.create_task(self._run_group(group), name=f"effects:{key}")
            self._lanes[key] = task
            task.add_done_callback(functools.partial(self._lane_done, key))
            started += 1
        return started

    async def run_once(self) -> int:
        """Run everything currently due and wait for it. Returns how many effects were attempted."""
        await self.dispatch()
        if not self._lanes:
            return 0
        counts = await asyncio.gather(*self._lanes.values())
        return sum(counts)

    def _lane_done(self, key: int | None, task: asyncio.Task[int]) -> None:
        if self._lanes.get(key) is task:
            del self._lanes[key]
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Effect lane crashed", extra={"match": key}, exc_info=error)
        if self.on_lane_done is not None:
            self.on_lane_done()

    async def drain(self, limit: int = 500) -> int:
        """Keep running until nothing is due, for tests and tooling."""
        total = 0
        for _ in range(limit):
            ran = await self.run_once()
            if ran == 0:
                return total
            total += ran
        raise RuntimeError("Outbox didn't drain: an effect is probably re-enqueuing itself.")

    async def _run_group(self, group: list[OutboxEffect]) -> int:
        ran = 0
        for effect in group:
            ran += 1
            if not await self._run_effect(effect):
                break  # Keep this match's effects in order: later ones wait for the retry.
        return ran

    async def _run_effect(self, effect: OutboxEffect) -> bool:
        handler = HANDLERS.get(effect.kind)
        if handler is None:
            await db(effects.mark_failed, effect.pk, f"No handler for {effect.kind}")
            logger.error("Unknown effect kind", extra={"kind": effect.kind})
            return True
        env = Env(self.gateway, effect, effect.community, effect.match)
        try:
            await handler(env)
        except (Skip, ChannelGone) as exc:
            await db(effects.mark_done, effect.pk, EffectStatus.SKIPPED)
            logger.debug("Effect skipped", extra={"kind": effect.kind, "why": str(exc)})
            return True
        except discord.HTTPException as exc:
            if exc.status in _PERMANENT_STATUS:
                await self._fail(effect, f"{exc.status} {exc.code}: {exc.text}")
                return True
            return await self._retry(effect, f"{exc.status} {exc.code}: {exc.text}")
        except Exception as exc:
            logger.exception("Effect failed", extra={"kind": effect.kind, "effect": effect.pk})
            return await self._retry(effect, repr(exc))
        await db(effects.mark_done, effect.pk)
        return True

    async def _retry(self, effect: OutboxEffect, error: str) -> bool:
        status = await db(effects.mark_retry, effect.pk, error)
        if status == EffectStatus.FAILED:
            await self._on_failed(effect, error)
            return True
        logger.warning("Effect will retry", extra={"kind": effect.kind, "error": error})
        return False

    async def _fail(self, effect: OutboxEffect, error: str) -> None:
        await db(effects.mark_failed, effect.pk, error)
        await self._on_failed(effect, error)

    async def _on_failed(self, effect: OutboxEffect, error: str) -> None:
        logger.error(
            "Effect failed for good",
            extra={
                "kind": effect.kind,
                "effect": effect.pk,
                "match": effect.match_id,
                "error": error,
            },
        )
        if effect.kind == "create_lobby" and effect.match_id is not None:
            # With no lobby, nobody can continue: cancel and put everyone back in line.
            await db(lobby.fail_lobby_setup, effect.match_id, "Discord couldn't create the lobby")
