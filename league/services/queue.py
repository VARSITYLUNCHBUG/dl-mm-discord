"""The FIFO queue. Every change takes the community row lock, so pops can never race joins."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from league.domain.engine import LIVE
from league.domain.errors import InvalidAction
from league.domain.mode_schema import parse_mode
from league.models import Community, Match, MatchParticipant, Player, QueueEntry
from league.services import lobby, penalties
from league.services.common import lock_community, now, render_queue

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class QueueStatus:
    size: int
    position: int | None
    match_id: int | None


def _position(community: Community, player: Player) -> int | None:
    players = list(
        QueueEntry.objects.filter(community=community).values_list("player_id", flat=True)
    )
    return players.index(player.pk) + 1 if player.pk in players else None


@transaction.atomic
def join(community_id: int, player: Player) -> QueueStatus:
    community = lock_community(community_id)
    live = MatchParticipant.objects.filter(player=player, is_active=True).first()
    if live is not None:
        raise InvalidAction(f"You're already in match #{live.match_id}.")
    penalty = penalties.active_penalty(community, player)
    if penalty is not None:
        raise InvalidAction(penalties.refusal_text(community, player, penalty))
    _entry, created = QueueEntry.objects.get_or_create(
        community=community, player=player, defaults={"joined_at": now()}
    )
    if not created:
        raise InvalidAction("You're already in the queue.")
    render_queue(community)
    match = try_pop(community)
    if match is not None and match.participants.filter(player=player).exists():
        return QueueStatus(size=0, position=None, match_id=match.pk)
    return status(community, player)


@transaction.atomic
def leave(community_id: int, player: Player) -> None:
    community = lock_community(community_id)
    deleted, _ = QueueEntry.objects.filter(community=community, player=player).delete()
    if not deleted:
        raise InvalidAction("You're not in the queue.")
    render_queue(community)


@transaction.atomic
def kick(community_id: int, player: Player) -> bool:
    community = lock_community(community_id)
    deleted, _ = QueueEntry.objects.filter(community=community, player=player).delete()
    if deleted:
        render_queue(community)
    return bool(deleted)


@transaction.atomic
def clear(community_id: int) -> int:
    community = lock_community(community_id)
    deleted, _ = QueueEntry.objects.filter(community=community).delete()
    render_queue(community)
    return deleted


def status(community: Community, player: Player) -> QueueStatus:
    live = MatchParticipant.objects.filter(player=player, is_active=True).first()
    return QueueStatus(
        size=QueueEntry.objects.filter(community=community).count(),
        position=_position(community, player),
        match_id=live.match_id if live else None,
    )


def try_pop(community: Community) -> Match | None:
    """Start a lobby if the queue is full and capacity allows. Requires the community lock."""
    mode = parse_mode(community.active_mode.body)
    live = Match.objects.filter(community=community, status__in=LIVE).count()
    if live >= community.max_concurrent_matches:
        return None
    entries = list(
        QueueEntry.objects.filter(community=community).select_related("player")[: mode.lobby_size]
    )
    if len(entries) < mode.lobby_size:
        return None
    return lobby.create_match(community, entries)


@transaction.atomic
def pop_if_ready(community_id: int) -> Match | None:
    return try_pop(lock_community(community_id))
