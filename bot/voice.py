"""Reads voice state from discord.py's cache for the priority list. No Members intent needed:
voice states carry their own member objects."""

from __future__ import annotations

import discord


def members_in(guild: discord.Guild, channel_id: int | None) -> dict[int, str]:
    """Who is in this voice channel: user ID to display name."""
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.VoiceChannel | discord.StageChannel):
        return {}
    found: dict[int, str] = {}
    for user_id in channel.voice_states:
        member = guild.get_member(user_id)
        found[user_id] = member.display_name if member else ""
    return found
