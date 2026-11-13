"""Bebot's named channel pings (/pingchannel, /ping) and the error log channel."""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from django.db import transaction

from league.domain.errors import InvalidAction, NotAllowed
from league.models import ChannelPing, Community
from league.services.common import now


def add(
    community_id: int,
    channel_id: int,
    name: str,
    mention: str,
    timer_minutes: int,
    required_role_id: int | None,
) -> ChannelPing:
    """Add a ping, or replace the one with the same name in this channel."""
    if timer_minutes < 1:
        raise InvalidAction("Timer must be at least 1 minute.")
    cleaned = name.strip().lower()
    if not cleaned:
        raise InvalidAction("Name cannot be empty.")
    ping, _created = ChannelPing.objects.update_or_create(
        channel_id=channel_id,
        name=cleaned[:50],
        defaults={
            "community_id": community_id,
            "mention": mention[:500],
            "timer_minutes": timer_minutes,
            "required_role_id": required_role_id,
            "last_ping_at": None,
        },
    )
    return ping


def remove(channel_id: int, name: str) -> bool:
    deleted, _ = ChannelPing.objects.filter(
        channel_id=channel_id, name=name.strip().lower()
    ).delete()
    return bool(deleted)


def in_channel(channel_id: int) -> list[ChannelPing]:
    return list(ChannelPing.objects.filter(channel_id=channel_id).order_by("name"))


def remaining_minutes(ping: ChannelPing, moment: datetime | None = None) -> float:
    """Minutes before this ping can be used again: 0 when it's ready."""
    if ping.last_ping_at is None:
        return 0
    elapsed = ((moment or now()) - ping.last_ping_at).total_seconds() / 60
    return max(0.0, ping.timer_minutes - elapsed)


def _options(pings: list[ChannelPing]) -> str:
    return ", ".join(f"`{ping.name}`" for ping in pings)


@transaction.atomic
def claim(channel_id: int, name: str | None, role_ids: Collection[int]) -> ChannelPing:
    """Check a /ping and start its cooldown, under a lock so two at once can't both go out."""
    pings = list(ChannelPing.objects.select_for_update().filter(channel_id=channel_id))
    if not pings:
        raise InvalidAction("This channel has no pings. An admin can add one with /pingchannel.")
    if name is None:
        if len(pings) > 1:
            raise InvalidAction(f"Pick which ping to use: {_options(pings)}")
        ping = pings[0]
    else:
        wanted = name.strip().lower()
        found = next((ping for ping in pings if ping.name == wanted), None)
        if found is None:
            raise InvalidAction(f"No ping named `{wanted}` here. Options: {_options(pings)}")
        ping = found
    if ping.required_role_id and ping.required_role_id not in role_ids:
        raise NotAllowed(f"You need the <@&{ping.required_role_id}> role to use this ping.")
    remaining = remaining_minutes(ping)
    if remaining > 0:
        raise InvalidAction(f"Ping `{ping.name}` on cooldown ({remaining:.0f}m left)")
    ping.last_ping_at = now()
    ping.save(update_fields=["last_ping_at"])
    return ping


# --- Error channel -------------------------------------------------------------------------


def set_error_channel(community_id: int, channel_id: int | None) -> None:
    Community.objects.filter(pk=community_id).update(error_channel_id=channel_id)


def error_channels() -> list[int]:
    rows = Community.objects.filter(error_channel_id__isnull=False)
    return [
        channel_id for channel_id in rows.values_list("error_channel_id", flat=True) if channel_id
    ]
