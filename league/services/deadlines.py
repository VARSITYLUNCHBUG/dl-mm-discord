"""Timeouts go through the same transitions as clicks, recorded with `source = timeout`."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction

from league.domain.engine import LIVE, Status
from league.domain.errors import DomainError
from league.models import CaptainHandoff, Community, Match
from league.models.matches import HandoffStatus
from league.services import draft, lobby, queue, results, subs
from league.services.common import lock_match, now

logger = logging.getLogger(__name__)

STARTUP_GRACE_SECONDS = 60

_HANDLERS = {
    Status.READY_CHECK: lobby.on_ready_timeout,
    Status.CAPTAIN_PHASE: lobby.on_captain_timeout,
    Status.PLAYER_DRAFT: draft.on_player_draft_timeout,
    Status.SIDE_SELECT: draft.on_side_timeout,
    Status.CHAMPION_DRAFT: draft.on_champion_draft_timeout,
    Status.LOBBY_CODE: draft.on_lobby_code_timeout,
    Status.IN_PROGRESS: results.on_in_progress_timeout,
    Status.AWAITING_CONFIRMATION: results.on_confirm_timeout,
    Status.DISPUTED: results.on_disputed_timeout,
}


def fire_match_deadline(match_id: int) -> bool:
    """Apply one match's timeout if it is still due. Returns whether anything happened."""
    with transaction.atomic():
        match = lock_match(match_id)
        # Re-check under the lock: a click may have got here first.
        if match.deadline_at is None or match.deadline_at > now() or match.status not in LIVE:
            return False
        _HANDLERS[match.status](match)
        return True


def fire_handoff_deadline(handoff_id: int) -> None:
    with transaction.atomic():
        handoff = CaptainHandoff.objects.select_related("match").get(pk=handoff_id)
        if handoff.status == HandoffStatus.PENDING and handoff.deadline_at <= now():
            lobby.expire_handoff(handoff)


def tick() -> int:
    """One poller pass. A failure in one match is logged and cannot block the others."""
    moment = now()
    fired = 0
    # A paused match's hand-offs wait too. They get time back when the sub arrives.
    handoffs = CaptainHandoff.objects.filter(
        status=HandoffStatus.PENDING, deadline_at__lte=moment, match__held_seconds__isnull=True
    )
    for handoff_id in handoffs.values_list("pk", flat=True):
        try:
            fire_handoff_deadline(handoff_id)
            fired += 1
        except DomainError as exc:
            logger.info(
                "Handoff deadline skipped", extra={"handoff": handoff_id, "why": exc.message}
            )
        except Exception:
            logger.exception("Handoff deadline failed", extra={"handoff": handoff_id})

    due = Match.objects.filter(status__in=LIVE, deadline_at__lte=moment)
    for match_id in due.values_list("pk", flat=True):
        try:
            fired += 1 if fire_match_deadline(match_id) else 0
        except DomainError as exc:
            logger.info("Match deadline skipped", extra={"match": match_id, "why": exc.message})
        except Exception:
            logger.exception("Match deadline failed", extra={"match": match_id})

    fired += subs.tick(moment)

    for community_id in Community.objects.values_list("pk", flat=True):
        try:
            queue.pop_if_ready(community_id)
        except Exception:
            logger.exception("Queue pop failed", extra={"community": community_id})
    return fired


def extend_past_due(grace_seconds: int = STARTUP_GRACE_SECONDS) -> int:
    """After downtime, give everyone time to react rather than firing a burst of auto-picks."""
    moment = now()
    new_deadline = moment + timedelta(seconds=grace_seconds)
    count = Match.objects.filter(status__in=LIVE, deadline_at__lt=new_deadline).update(
        deadline_at=new_deadline
    )
    count += CaptainHandoff.objects.filter(
        status=HandoffStatus.PENDING, deadline_at__lt=new_deadline
    ).update(deadline_at=new_deadline)
    count += subs.extend_past_due(new_deadline)
    return count
