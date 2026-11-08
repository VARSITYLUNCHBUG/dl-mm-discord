"""Stateless component routing.

Everything a click needs is in its custom_id, and the handler reads the rest from the database, so
buttons keep working after a restart and a stale click fails cleanly. Buttons and selects need
separate templates because discord.py keys dynamic items by pattern alone.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import discord
from discord import ButtonStyle, Interaction, SelectOption

from league.domain.errors import DomainError

logger = logging.getLogger(__name__)

BUTTON_PREFIX = "dl:v1"
SELECT_PREFIX = "dl:v1s"
USER_SELECT_PREFIX = "dl:v1u"
_TAIL = r":(?P<action>[a-z_]+):(?P<match>\d+):(?P<arg>[\w.-]*)"
BUTTON_TEMPLATE = re.escape(BUTTON_PREFIX) + _TAIL
SELECT_TEMPLATE = re.escape(SELECT_PREFIX) + _TAIL
USER_SELECT_TEMPLATE = re.escape(USER_SELECT_PREFIX) + _TAIL


@dataclass(frozen=True, slots=True)
class Context:
    interaction: Interaction[Any]
    action: str
    match_id: int
    arg: str
    values: tuple[str, ...] = ()

    @property
    def bot(self) -> Any:
        return self.interaction.client


Handler = Callable[[Context], Awaitable[None]]
_handlers: dict[str, Handler] = {}


def handles(action: str) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        if action in _handlers:
            raise ValueError(f"Duplicate handler for {action!r}")
        _handlers[action] = handler
        return handler

    return register


def registered_actions() -> frozenset[str]:
    return frozenset(_handlers)


async def reply(interaction: Interaction[Any], text: str) -> None:
    """Ephemeral text, whichever state the interaction is in."""
    allowed = discord.AllowedMentions.none()
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=allowed)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=allowed)


async def guarded(interaction: Interaction[Any], call: Callable[[], Awaitable[None]]) -> None:
    """Show expected refusals to the user. Anything else is logged and never leaks details."""
    try:
        await call()
    except DomainError as exc:
        await reply(interaction, exc.message)
    except Exception:
        logger.exception("Interaction failed", extra={"interaction": interaction.id})
        await reply(interaction, "Something went wrong on our side. Please try again.")
    finally:
        wake = getattr(interaction.client, "wake_effects", None)
        if wake is not None:
            wake()


async def _dispatch(context: Context) -> None:
    handler = _handlers.get(context.action)
    if handler is None:
        await reply(context.interaction, "That button is no longer available.")
        return
    await guarded(context.interaction, lambda: handler(context))


def button_id(action: str, match_id: int = 0, arg: str | int = "") -> str:
    return f"{BUTTON_PREFIX}:{action}:{match_id}:{arg}"


def select_id(action: str, match_id: int = 0, arg: str | int = "") -> str:
    return f"{SELECT_PREFIX}:{action}:{match_id}:{arg}"


class RouteButton(discord.ui.DynamicItem[discord.ui.Button[Any]], template=BUTTON_TEMPLATE):
    def __init__(
        self,
        action: str,
        match_id: int = 0,
        arg: str | int = "",
        *,
        label: str | None = None,
        style: ButtonStyle = ButtonStyle.secondary,
        disabled: bool = False,
        emoji: discord.PartialEmoji | None = None,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                disabled=disabled,
                emoji=emoji,
                custom_id=button_id(action, match_id, arg),
            )
        )
        self.action = action
        self.match_id = match_id
        self.arg = str(arg)

    @classmethod
    async def from_custom_id(
        cls, interaction: Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str]
    ) -> RouteButton:
        return cls(
            match["action"],
            int(match["match"]),
            match["arg"],
            label=getattr(item, "label", None),
            style=getattr(item, "style", ButtonStyle.secondary),
        )

    async def callback(self, interaction: Interaction[Any]) -> None:
        await _dispatch(Context(interaction, self.action, self.match_id, self.arg))


class RouteSelect(discord.ui.DynamicItem[discord.ui.Select[Any]], template=SELECT_TEMPLATE):
    def __init__(
        self,
        action: str,
        match_id: int = 0,
        arg: str | int = "",
        *,
        options: list[SelectOption] | None = None,
        placeholder: str | None = None,
        min_values: int = 1,
        max_values: int = 1,
    ) -> None:
        super().__init__(
            discord.ui.Select(
                custom_id=select_id(action, match_id, arg),
                options=options or [SelectOption(label="-", value="-")],
                placeholder=placeholder,
                min_values=min_values,
                max_values=max_values,
            )
        )
        self.action = action
        self.match_id = match_id
        self.arg = str(arg)

    @classmethod
    async def from_custom_id(
        cls, interaction: Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str]
    ) -> RouteSelect:
        return cls(match["action"], int(match["match"]), match["arg"])

    async def callback(self, interaction: Interaction[Any]) -> None:
        data = cast(dict[str, Any], interaction.data or {})
        values = tuple(str(value) for value in data.get("values", ()))
        await _dispatch(Context(interaction, self.action, self.match_id, self.arg, values))


def user_select_id(action: str, match_id: int = 0, arg: str | int = "") -> str:
    return f"{USER_SELECT_PREFIX}:{action}:{match_id}:{arg}"


@dataclass(frozen=True, slots=True)
class PickedUser:
    """A member chosen in a user select, read from the interaction's resolved data."""

    id: int
    name: str
    is_bot: bool


def picked_users(interaction: Interaction[Any]) -> list[PickedUser]:
    """Names come from what Discord sends with the click, so no member intent is needed."""
    data = cast(dict[str, Any], interaction.data or {})
    resolved = data.get("resolved", {})
    users = resolved.get("users", {})
    members = resolved.get("members", {})
    picked = []
    for raw in data.get("values", ()):
        user = users.get(str(raw), {})
        member = members.get(str(raw), {})
        name = member.get("nick") or user.get("global_name") or user.get("username") or str(raw)
        picked.append(PickedUser(int(raw), str(name), bool(user.get("bot"))))
    return picked


class RouteUserSelect(
    discord.ui.DynamicItem[discord.ui.UserSelect[Any]], template=USER_SELECT_TEMPLATE
):
    def __init__(
        self, action: str, match_id: int = 0, arg: str | int = "", *, placeholder: str | None = None
    ) -> None:
        super().__init__(
            discord.ui.UserSelect(
                custom_id=user_select_id(action, match_id, arg), placeholder=placeholder
            )
        )
        self.action = action
        self.match_id = match_id
        self.arg = str(arg)

    @classmethod
    async def from_custom_id(
        cls, interaction: Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str]
    ) -> RouteUserSelect:
        return cls(match["action"], int(match["match"]), match["arg"])

    async def callback(self, interaction: Interaction[Any]) -> None:
        data = cast(dict[str, Any], interaction.data or {})
        values = tuple(str(value) for value in data.get("values", ()))
        await _dispatch(Context(interaction, self.action, self.match_id, self.arg, values))
