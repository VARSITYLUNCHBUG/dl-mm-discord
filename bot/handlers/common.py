from __future__ import annotations

from typing import Any

import discord
from discord import Interaction

from bot.db import db
from league.domain.errors import InvalidAction, NotAllowed
from league.models import Community, Match, Player
from league.services import common
from league.services import community as community_service


async def actor(interaction: Interaction[Any]) -> Player:
    """The Player for whoever clicked. First interaction creates the row."""
    user = interaction.user
    return await db(common.get_or_create_player, user.id, user.display_name)


async def community_of(interaction: Interaction[Any]) -> Community:
    guild_id = interaction.guild_id
    community = await db(community_service.get, guild_id) if guild_id else None
    if community is None:
        raise InvalidAction("This server isn't set up yet. An admin needs to run /setup.")
    return community


def _community_of_match(match_id: int) -> Community:
    return Match.objects.select_related("community").get(pk=match_id).community


def _holds_staff(user: discord.abc.User, community: Community) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return bool(community.staff_role_id) and any(
        role.id == community.staff_role_id for role in user.roles
    )


async def is_staff(interaction: Interaction[Any], community: Community) -> bool:
    return _holds_staff(interaction.user, community)


async def require_staff(interaction: Interaction[Any], community: Community | None = None) -> None:
    """Administrators, or anyone holding the configured staff role."""
    community = community or await community_of(interaction)
    if not _holds_staff(interaction.user, community):
        raise NotAllowed("Only staff can do that.")


async def require_staff_for_match(interaction: Interaction[Any], match_id: int) -> None:
    await require_staff(interaction, await db(_community_of_match, match_id))
