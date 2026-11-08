"""Escalating penalties that decay. Automatic for missed ready checks, otherwise manual."""

from __future__ import annotations

from datetime import datetime, timedelta

from league.models import Community, Match, Penalty, Player
from league.models.moderation import PenaltyKind
from league.services.common import now


def active_penalty(community: Community, player: Player) -> Penalty | None:
    return (
        Penalty.objects.filter(
            community=community, player=player, revoked_at__isnull=True, expires_at__gt=now()
        )
        .order_by("-expires_at")
        .first()
    )


def _ladder(community: Community, kind: str) -> list[int]:
    ladders = community.penalty_policy.get("ladders", {})
    return list(ladders.get(kind, [60]))


def _tier_for(community: Community, player: Player, kind: str) -> int:
    decay_days = int(community.penalty_policy.get("decay_days", 14))
    recent = Penalty.objects.filter(
        community=community,
        player=player,
        kind=kind,
        revoked_at__isnull=True,
        starts_at__gte=now() - timedelta(days=decay_days),
    ).count()
    return recent + 1


def issue(
    community: Community,
    player: Player,
    kind: str,
    *,
    match: Match | None = None,
    reason: str = "",
    issued_by: Player | None = None,
    minutes: int | None = None,
) -> Penalty:
    """Issue a penalty. Without `minutes`, the duration comes from the community's ladder."""
    tier = _tier_for(community, player, kind)
    if minutes is None:
        ladder = _ladder(community, kind)
        minutes = ladder[min(tier, len(ladder)) - 1]
    starts_at: datetime = now()
    return Penalty.objects.create(
        community=community,
        player=player,
        match=match,
        kind=kind,
        tier=tier,
        starts_at=starts_at,
        expires_at=starts_at + timedelta(minutes=minutes),
        reason=reason[:500],
        issued_by=issued_by,
    )


def revoke_active(community: Community, player: Player) -> int:
    return Penalty.objects.filter(
        community=community, player=player, revoked_at__isnull=True, expires_at__gt=now()
    ).update(revoked_at=now())


__all__ = ["PenaltyKind", "active_penalty", "issue", "revoke_active"]
