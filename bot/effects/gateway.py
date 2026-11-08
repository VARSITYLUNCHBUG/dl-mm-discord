"""Everything the effect handlers need from Discord. A protocol, so tests can run the whole flow
against a fake with no network."""

from __future__ import annotations

import contextlib
import io
from collections.abc import Iterable
from typing import Any, Protocol

import discord


class ChannelGone(Exception):
    """The target channel or message no longer exists: treat deletion and edits as done."""


class Gateway(Protocol):
    async def create_text_channel(
        self,
        guild_id: int,
        category_id: int | None,
        name: str,
        member_ids: Iterable[int],
        staff_role_id: int | None,
    ) -> int: ...

    async def create_voice_channel(
        self,
        guild_id: int,
        category_id: int | None,
        name: str,
        member_ids: Iterable[int],
        staff_role_id: int | None,
    ) -> int: ...

    def voice_member_ids(self, guild_id: int, channel_id: int | None) -> set[int]: ...

    async def move_member(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        """False if the member isn't in voice (Discord error 40032). Not a failure."""
        ...

    async def create_invite(self, guild_id: int, channel_id: int, max_age: int) -> str | None: ...

    async def send_dm(self, user_id: int, view: discord.ui.LayoutView) -> bool:
        """False if the user can't be messaged (Discord error 50007). Not a failure."""
        ...

    async def send_view(
        self,
        channel_id: int,
        view: discord.ui.LayoutView,
        *,
        file: tuple[str, bytes] | None = None,
    ) -> int: ...

    async def edit_view(self, channel_id: int, message_id: int, view: Any) -> None: ...

    async def send_text(
        self,
        channel_id: int,
        text: str,
        *,
        user_ids: Iterable[int] = (),
        role_id: int | None = None,
    ) -> int: ...

    async def latest_message_id(self, channel_id: int) -> int | None: ...

    async def delete_message(self, channel_id: int, message_id: int) -> None: ...

    async def delete_channel(self, channel_id: int) -> None: ...

    async def grant_member(self, channel_id: int, user_id: int) -> None: ...

    async def revoke_member(self, channel_id: int, user_id: int) -> None: ...

    async def set_voice_status(self, channel_id: int, text: str) -> None: ...

    async def fetch_attachment(
        self, channel_id: int, message_id: int, attachment_id: int
    ) -> tuple[str, bytes] | None: ...


TEXT_MEMBER = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    attach_files=True,
    embed_links=True,
)
VOICE_MEMBER = discord.PermissionOverwrite(
    view_channel=True,
    connect=True,
    speak=True,
    use_voice_activation=True,
)
HIDDEN = discord.PermissionOverwrite(view_channel=False, connect=False)
# From 16 November 2026, a bot without VIEW_CHANNEL has channels obfuscated or left out of
# listings, so every channel the bot creates must grant the bot its own access.
# Don't add manage_permissions here: Discord refuses (50013) an overwrite granting it unless the
# bot is an Administrator. The server-wide Manage Roles permission already lets it edit overwrites.
SELF_TEXT = discord.PermissionOverwrite(
    view_channel=True,
    send_messages=True,
    read_message_history=True,
    attach_files=True,
    embed_links=True,
    manage_channels=True,
)
SELF_VOICE = discord.PermissionOverwrite(
    view_channel=True,
    connect=True,
    speak=True,
    use_voice_activation=True,
    move_members=True,
    manage_channels=True,
)


def _member(user_id: int) -> discord.Object:
    return discord.Object(id=user_id, type=discord.Member)


class DiscordGateway:
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    def _guild(self, guild_id: int) -> discord.Guild:
        guild = self.client.get_guild(guild_id)
        if guild is None:
            raise ChannelGone(f"Guild {guild_id} isn't available.")
        return guild

    def _overwrites(
        self,
        guild: discord.Guild,
        member_ids: Iterable[int],
        staff_role_id: int | None,
        member_perms: discord.PermissionOverwrite,
        self_perms: discord.PermissionOverwrite,
    ) -> dict[Any, discord.PermissionOverwrite]:
        overwrites: dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: HIDDEN,
            _member(self.client.user.id): self_perms,  # type: ignore[union-attr]
        }
        if staff_role_id:
            overwrites[discord.Object(id=staff_role_id, type=discord.Role)] = member_perms
        for user_id in member_ids:
            overwrites[_member(user_id)] = member_perms
        return overwrites

    def _category(self, guild: discord.Guild, category_id: int | None) -> Any:
        return guild.get_channel(category_id) if category_id else None

    async def create_text_channel(
        self,
        guild_id: int,
        category_id: int | None,
        name: str,
        member_ids: Iterable[int],
        staff_role_id: int | None,
    ) -> int:
        guild = self._guild(guild_id)
        channel = await guild.create_text_channel(
            name,
            category=self._category(guild, category_id),
            overwrites=self._overwrites(guild, member_ids, staff_role_id, TEXT_MEMBER, SELF_TEXT),
        )
        return channel.id

    async def create_voice_channel(
        self,
        guild_id: int,
        category_id: int | None,
        name: str,
        member_ids: Iterable[int],
        staff_role_id: int | None,
    ) -> int:
        guild = self._guild(guild_id)
        channel = await guild.create_voice_channel(
            name,
            category=self._category(guild, category_id),
            overwrites=self._overwrites(guild, member_ids, staff_role_id, VOICE_MEMBER, SELF_VOICE),
        )
        return channel.id

    def voice_member_ids(self, guild_id: int, channel_id: int | None) -> set[int]:
        if channel_id is None:
            return set()
        channel = self._guild(guild_id).get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel | discord.StageChannel):
            return set()
        # A low-level mapping that works without the privileged Members intent.
        return set(channel.voice_states)

    async def move_member(self, guild_id: int, user_id: int, channel_id: int) -> bool:
        try:
            # A plain PATCH: no member cache needed, one request per move.
            await self.client.http.edit_member(guild_id, user_id, channel_id=channel_id)
        except discord.HTTPException as exc:
            if exc.code == 40032:
                return False
            raise
        return True

    async def create_invite(self, guild_id: int, channel_id: int, max_age: int) -> str | None:
        channel = self._guild(guild_id).get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return None
        invite = await channel.create_invite(max_age=max_age, max_uses=0, unique=False)
        return invite.url

    async def send_dm(self, user_id: int, view: discord.ui.LayoutView) -> bool:
        try:
            channel = await self.client.create_dm(discord.Object(id=user_id))
            await channel.send(view=view)
        except discord.Forbidden:
            return False
        except discord.HTTPException as exc:
            if exc.code == 50007:
                return False
            raise
        return True

    async def _messageable(self, channel_id: int) -> Any:
        channel = self.client.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.client.fetch_channel(channel_id)
        except discord.NotFound as exc:
            raise ChannelGone(str(channel_id)) from exc

    async def send_view(
        self,
        channel_id: int,
        view: discord.ui.LayoutView,
        *,
        file: tuple[str, bytes] | None = None,
    ) -> int:
        channel = await self._messageable(channel_id)
        kwargs: dict[str, Any] = {"view": view, "allowed_mentions": discord.AllowedMentions.none()}
        if file is not None:
            kwargs["file"] = discord.File(io.BytesIO(file[1]), filename=file[0])
        try:
            message = await channel.send(**kwargs)
        except discord.NotFound as exc:
            raise ChannelGone(str(channel_id)) from exc
        return message.id

    async def edit_view(self, channel_id: int, message_id: int, view: Any) -> None:
        channel = await self._messageable(channel_id)
        try:
            await channel.get_partial_message(message_id).edit(
                view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.NotFound as exc:
            raise ChannelGone(str(message_id)) from exc

    async def send_text(
        self,
        channel_id: int,
        text: str,
        *,
        user_ids: Iterable[int] = (),
        role_id: int | None = None,
    ) -> int:
        channel = await self._messageable(channel_id)
        ids = list(user_ids)
        prefix = " ".join([*(f"<@{uid}>" for uid in ids), *([f"<@&{role_id}>"] if role_id else [])])
        allowed = discord.AllowedMentions(
            users=[discord.Object(id=uid) for uid in ids],
            roles=[discord.Object(id=role_id)] if role_id else False,
            everyone=False,
        )
        content = f"{prefix} {text}".strip()[:2000]
        try:
            message = await channel.send(content, allowed_mentions=allowed)
        except discord.NotFound as exc:
            raise ChannelGone(str(channel_id)) from exc
        return message.id

    async def latest_message_id(self, channel_id: int) -> int | None:
        channel = await self._messageable(channel_id)
        try:
            async for message in channel.history(limit=1):
                return message.id
        except discord.NotFound as exc:
            raise ChannelGone(str(channel_id)) from exc
        return None

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        # A bot can always delete its own messages, so this needs no Manage Messages permission.
        channel = await self._messageable(channel_id)
        with contextlib.suppress(discord.NotFound):  # Already gone counts as deleted.
            await channel.get_partial_message(message_id).delete()

    async def delete_channel(self, channel_id: int) -> None:
        try:
            channel = await self._messageable(channel_id)
            await channel.delete()
        except (discord.NotFound, ChannelGone):
            return  # Already gone counts as deleted.

    async def grant_member(self, channel_id: int, user_id: int) -> None:
        channel = await self._messageable(channel_id)
        perms = VOICE_MEMBER if isinstance(channel, discord.VoiceChannel) else TEXT_MEMBER
        await channel.set_permissions(_member(user_id), overwrite=perms)

    async def revoke_member(self, channel_id: int, user_id: int) -> None:
        channel = await self._messageable(channel_id)
        await channel.set_permissions(_member(user_id), overwrite=None)

    async def set_voice_status(self, channel_id: int, text: str) -> None:
        channel = await self._messageable(channel_id)
        if isinstance(channel, discord.VoiceChannel):
            await channel.edit(status=text[:500])

    async def fetch_attachment(
        self, channel_id: int, message_id: int, attachment_id: int
    ) -> tuple[str, bytes] | None:
        channel = await self._messageable(channel_id)
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return None
        for attachment in message.attachments:
            if attachment.id == attachment_id:
                return attachment.filename, await attachment.read()
        return None
