"""Bebot's voice priority list (/priolist): who has waited longest in the Waiting Room.

It follows the match lifecycle instead of tracking every voice move:

- Joining the Waiting Room puts you on the list. Leaving starts a 5 minute grace period; come
  back in time and you keep your place.
- Being put in a match takes you off. While the match is live, voice moves are ignored (and the
  queue refuses you anyway).
- When the match ends, cleanup moves everyone back to the Waiting Room, which puts them on the
  back of the list. That replaces Bebot's "recently played" timer.

The Waiting Room is the one set up for the match queue (/setup), so there's nothing extra to
configure.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import transaction

from league.models import Community, MatchParticipant, OutboxEffect, Player, PrioEntry
from league.models.outbox import EffectStatus
from league.services.common import add_effect, now

logger = logging.getLogger(__name__)

GRACE_SECONDS = 300


def waiting_room(community_id: int) -> int | None:
    return (
        Community.objects.filter(pk=community_id)
        .values_list("waiting_room_channel_id", flat=True)
        .first()
    )


def tracked_communities() -> list[Community]:
    return list(Community.objects.filter(waiting_room_channel_id__isnull=False))


def in_match(user_ids: Collection[int]) -> set[int]:
    """Which of these Discord users are in a live match."""
    rows = MatchParticipant.objects.filter(
        is_active=True, player__discord_user_id__in=list(user_ids)
    )
    return set(rows.values_list("player__discord_user_id", flat=True))


def request_render(community_id: int) -> None:
    community = Community.objects.get(pk=community_id)
    if not community.prio_channel_id:
        return
    pending = OutboxEffect.objects.filter(
        community=community, kind="render_prio", status=EffectStatus.PENDING, attempts=0
    )
    if not pending.exists():
        add_effect(community, "render_prio")


def set_display(community_id: int, channel_id: int) -> None:
    """/priolist: post a fresh list in this channel. The old message is left alone."""
    Community.objects.filter(pk=community_id).update(
        prio_channel_id=channel_id, prio_message_id=None
    )
    request_render(community_id)


def save_display(community_id: int, channel_id: int, message_id: int) -> None:
    Community.objects.filter(pk=community_id, prio_channel_id=channel_id).update(
        prio_message_id=message_id
    )


# --- Changes --------------------------------------------------------------------------------


def _join(community_id: int, user_id: int, name: str, moment: datetime) -> None:
    """In the Waiting Room: keep their place if they have one, else go on the back."""
    entry, created = PrioEntry.objects.select_for_update().get_or_create(
        community_id=community_id,
        discord_user_id=user_id,
        defaults={"display_name": name[:100] or "?", "queued_at": moment},
    )
    if created:
        return
    entry.grace_until = None
    if name:
        entry.display_name = name[:100]
    entry.save(update_fields=["grace_until", "display_name"])


def _start_grace(community_id: int, user_id: int, moment: datetime) -> None:
    PrioEntry.objects.filter(
        community_id=community_id, discord_user_id=user_id, grace_until__isnull=True
    ).update(grace_until=moment + timedelta(seconds=GRACE_SECONDS))


@transaction.atomic
def on_voice_state(
    community_id: int, user_id: int, name: str, before_id: int | None, after_id: int | None
) -> bool:
    """Someone moved in voice. Returns whether the list may have changed."""
    room = waiting_room(community_id)
    if room is None or room not in (before_id, after_id):
        return False
    if in_match([user_id]):
        # Matched players aren't on the list, wherever they go, until the match ends.
        deleted, _ = PrioEntry.objects.filter(
            community_id=community_id, discord_user_id=user_id
        ).delete()
        if deleted:
            request_render(community_id)
        return bool(deleted)
    moment = now()
    if after_id == room:
        _join(community_id, user_id, name, moment)
    else:
        _start_grace(community_id, user_id, moment)
    request_render(community_id)
    return True


def drop_players(community_id: int, user_ids: Collection[int]) -> None:
    """They've been put in a match, so they're off the list. Runs inside the match change."""
    deleted, _ = PrioEntry.objects.filter(
        community_id=community_id, discord_user_id__in=list(user_ids)
    ).delete()
    if deleted:
        request_render(community_id)


@transaction.atomic
def remove(community_id: int, user_id: int) -> bool:
    """/removeprio. False if they weren't on the list."""
    deleted, _ = PrioEntry.objects.filter(
        community_id=community_id, discord_user_id=user_id
    ).delete()
    if deleted:
        request_render(community_id)
    return bool(deleted)


@transaction.atomic
def reconcile(community_id: int, waiting: Mapping[int, str]) -> None:
    """Match the list to who is in the Waiting Room now.

    Runs after a restart, and after a match's cleanup: a player who was already sitting in the
    Waiting Room gets no voice event to put them back on. `waiting` maps the user IDs in the
    Waiting Room to their display names; a blank name is looked up from their player record.
    """
    moment = now()
    busy = in_match(waiting)
    unnamed = [user_id for user_id, name in waiting.items() if not name]
    known = dict(
        Player.objects.filter(discord_user_id__in=unnamed).values_list(
            "discord_user_id", "display_name"
        )
    )
    for user_id, name in waiting.items():
        if user_id not in busy:
            _join(community_id, user_id, name or known.get(user_id, ""), moment)
    away = PrioEntry.objects.filter(community_id=community_id, grace_until__isnull=True).exclude(
        discord_user_id__in=list(waiting)
    )
    away.update(grace_until=moment + timedelta(seconds=GRACE_SECONDS))
    request_render(community_id)


# --- Timers ---------------------------------------------------------------------------------


def tick(moment: datetime) -> int:
    """Take off anyone whose grace period has run out."""
    expired = PrioEntry.objects.filter(grace_until__lte=moment)
    changed = set(expired.values_list("community_id", flat=True))
    expired.delete()
    for community_id in changed:
        request_render(community_id)
    return len(changed)


def refresh_all() -> int:
    """Wait times count up in minutes, so the bot re-renders lists with anyone on them."""
    ids = set(PrioEntry.objects.values_list("community_id", flat=True))
    for community_id in ids:
        request_render(community_id)
    return len(ids)


# --- Read model -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrioLine:
    status: str
    name: str
    minutes: int
    grace_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class PrioView:
    lines: list[PrioLine]


def prio_view(community_id: int) -> PrioView:
    """Longest wait first. Anyone in a live match is left off, in case a move was missed."""
    moment = now()
    entries = list(PrioEntry.objects.filter(community_id=community_id).order_by("queued_at", "id"))
    busy = in_match([entry.discord_user_id for entry in entries])
    lines = []
    for entry in entries:
        if entry.discord_user_id in busy:
            continue
        minutes = int((moment - entry.queued_at).total_seconds()) // 60
        if entry.grace_until is not None:
            left = int((entry.grace_until - moment).total_seconds()) // 60
            lines.append(PrioLine("⏳", entry.display_name, minutes, max(0, left)))
        else:
            lines.append(PrioLine("🟢", entry.display_name, minutes))
    return PrioView(lines)
