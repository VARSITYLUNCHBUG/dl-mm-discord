"""Bebot's /seterrorchannel: ERROR logs are posted to a Discord channel as well as stdout.

The handler never touches the database: the bot loads the channel list at startup and the
commands update it. Records can come from any thread, so posting is scheduled onto the bot's loop.
A failed post is dropped quietly, and a burst is capped, so the handler can't feed itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import traceback
from typing import Any

import discord

MAX_LENGTH = 1950
BURST_LIMIT = 10
BURST_WINDOW_SECONDS = 60.0


class DiscordChannelLogHandler(logging.Handler):
    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level=level)
        self.setFormatter(logging.Formatter("**[%(levelname)s]** `%(name)s` — %(message)s"))
        self._client: discord.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._channels: set[int] = set()
        self._pending: list[logging.LogRecord] = []
        self._sent: list[float] = []

    @property
    def channels(self) -> set[int]:
        return set(self._channels)

    def set_channels(self, channel_ids: list[int] | set[int]) -> None:
        self._channels = set(channel_ids)

    def add_channel(self, channel_id: int) -> None:
        self._channels.add(channel_id)

    def remove_channel(self, channel_id: int | None) -> None:
        if channel_id is not None:
            self._channels.discard(channel_id)

    def bind(self, client: discord.Client, loop: asyncio.AbstractEventLoop) -> None:
        """Start posting. Records logged before the bot was ready are sent now."""
        self._client = client
        self._loop = loop
        pending, self._pending = self._pending, []
        for record in pending:
            self._dispatch(record)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._client is None or self._loop is None:
                if len(self._pending) < 200:
                    self._pending.append(record)
                return
            self._dispatch(record)
        except Exception:  # A log handler must never raise.
            return

    def _allowed(self) -> bool:
        moment = time.monotonic()
        self._sent = [at for at in self._sent if moment - at < BURST_WINDOW_SECONDS]
        if len(self._sent) >= BURST_LIMIT:
            return False
        self._sent.append(moment)
        return True

    def _dispatch(self, record: logging.LogRecord) -> None:
        if not self._channels or self._loop is None or not self._loop.is_running():
            return
        if not self._allowed():
            return
        body = format_record(self, record)
        asyncio.run_coroutine_threadsafe(self._send(body), self._loop)

    async def _send(self, body: str) -> None:
        client = self._client
        if client is None:
            return
        for channel_id in list(self._channels):
            with contextlib.suppress(Exception):
                channel: Any = client.get_channel(channel_id) or await client.fetch_channel(
                    channel_id
                )
                await channel.send(body, allowed_mentions=discord.AllowedMentions.none())


def format_record(handler: logging.Handler, record: logging.LogRecord) -> str:
    """Bebot's format: the level, logger and message, then the traceback in a code block."""
    record.message = record.getMessage()
    header = (handler.formatter or logging.Formatter()).formatMessage(record)
    body = header
    if record.exc_info:
        trace = "".join(traceback.format_exception(*record.exc_info))
        body = f"{header}\n```\n{trace}\n```"
    if len(body) > MAX_LENGTH:
        body = body[:MAX_LENGTH] + "\n…(truncated)"
    return body


discord_log_handler = DiscordChannelLogHandler()
