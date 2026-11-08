"""Helpers every service shares: the clock, effect writing, transitions and player lookup."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import QuerySet
from django.utils import timezone

from league.domain.engine import can_transition
from league.domain.errors import InvalidAction, StaleAction
from league.domain.mode_schema import DraftMode, parse_mode
from league.models import (
    Community,
    Match,
    MatchParticipant,
    MatchTransition,
    OutboxEffect,
    Player,
    PlayerRating,
    Season,
    SubOffer,
)
from league.models.outbox import EffectStatus
from league.models.subs import UNRESOLVED, OfferStatus, SubStatus


def now() -> datetime:
    """The only clock services use, so tests can move time."""
    return timezone.now()


def seconds_from_now(seconds: int) -> datetime:
    return now() + timedelta(seconds=seconds)


def mode_of(match: Match) -> DraftMode:
    return parse_mode(match.mode_version.body)


def get_or_create_player(discord_user_id: int, display_name: str) -> Player:
    player, created = Player.objects.get_or_create(
        discord_user_id=discord_user_id, defaults={"display_name": display_name[:100]}
    )
    if not created and display_name and player.display_name != display_name[:100]:
        player.display_name = display_name[:100]
        player.save(update_fields=["display_name", "updated_at"])
    return player


def open_season(community: Community) -> Season:
    season = Season.objects.filter(community=community, ended_at__isnull=True).first()
    if season is None:
        raise InvalidAction("No season is open. Ask staff to run /setup.")
    return season


def rating_of(season: Season, player_id: int) -> tuple[int, int]:
    """(rating, games) for a player, falling back to the season's start rating."""
    row = PlayerRating.objects.filter(season=season, player_id=player_id).first()
    if row is None:
        return season.start_rating, 0
    return row.rating, row.games


def lock_match(match_id: int) -> Match:
    """Take the row lock every state change goes through. Call inside transaction.atomic()."""
    match = (
        Match.objects.select_for_update(of=("self",))
        .select_related("mode_version", "season", "community")
        .filter(pk=match_id)
        .first()
    )
    if match is None:
        raise StaleAction("That match no longer exists.")
    return match


def lock_community(community_id: int) -> Community:
    return Community.objects.select_for_update().get(pk=community_id)


def transition(
    match: Match,
    target: str,
    *,
    source: str,
    actor: Player | None = None,
    deadline_at: datetime | None = None,
) -> None:
    """Move a locked match to a new status, recording the change."""
    if not can_transition(match.status, target):
        raise StaleAction("This match has already moved on.")
    MatchTransition.objects.create(
        match=match,
        from_status=match.status,
        to_status=target,
        source=source,
        actor=actor,
        created_at=now(),
    )
    match.status = target
    match.deadline_at = deadline_at
    match.version += 1
    clear_notices(match)


def save_transition(match: Match, *extra_fields: str) -> None:
    """Persist a transition() and any fields changed alongside it."""
    match.save(update_fields=["status", "deadline_at", "version", *extra_fields])


def bump(match: Match, *fields: str) -> None:
    """Save changes to a locked match that stay within its current status."""
    match.version += 1
    match.save(update_fields=[*fields, "version"])


def add_effect(
    community: Community,
    kind: str,
    payload: dict[str, object] | None = None,
    *,
    match: Match | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
) -> None:
    created_at = now()
    if dedupe_key is not None:
        OutboxEffect.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults={
                "community": community,
                "match": match,
                "kind": kind,
                "payload": payload or {},
                "run_after": run_after or created_at,
                "created_at": created_at,
            },
        )
        return
    OutboxEffect.objects.create(
        community=community,
        match=match,
        kind=kind,
        payload=payload or {},
        run_after=run_after or created_at,
        created_at=created_at,
    )


def request_render(community: Community, kind: str, match: Match | None = None) -> None:
    """Ask for a re-render. Renders read the latest state, so pending ones coalesce into one."""
    pending = OutboxEffect.objects.filter(
        community=community, kind=kind, match=match, status=EffectStatus.PENDING, attempts=0
    )
    if not pending.exists():
        add_effect(community, kind, match=match)


def clear_notices(match: Match) -> None:
    """Remove the notices posted so far. Effects run in order, so later ones are unaffected."""
    add_effect(match.community, "clear_notices", match=match)


def render_board(match: Match) -> None:
    request_render(match.community, "render_board", match)


def render_queue(community: Community) -> None:
    request_render(community, "render_queue")


def active_participants(match: Match) -> QuerySet[MatchParticipant]:
    return match.participants.filter(is_active=True).select_related("player")


def roster(match: Match) -> QuerySet[MatchParticipant]:
    """Everyone who counts for this match's result, including after it has ended."""
    return match.participants.filter(removed_reason="").exclude(team="").select_related("player")


def captain_of(match: Match, team: str) -> MatchParticipant:
    return match.participants.select_related("player").get(
        team=team, is_captain=True, removed_reason=""
    )


def on_hold(match: Match) -> bool:
    """A sub slot is open, so the match's timer is parked and its actions wait."""
    return match.held_seconds is not None


def require_not_on_hold(match: Match) -> None:
    if on_hold(match):
        raise InvalidAction("The match is paused until the sub arrives.")


def close_subs(match: Match) -> None:
    """End every unfinished sub request, for a match that is ending or has its result in.

    Leaves the timer to the caller, which is about to replace it with a transition.
    """
    moment = now()
    SubOffer.objects.filter(request__match=match, status=OfferStatus.PENDING).update(
        status=OfferStatus.WITHDRAWN, resolved_at=moment
    )
    match.sub_requests.filter(status__in=UNRESOLVED).update(
        status=SubStatus.CANCELLED, resolved_at=moment
    )
    if match.held_seconds is not None:
        match.held_seconds = None
        match.save(update_fields=["held_seconds"])


def require_participant(match: Match, player: Player) -> MatchParticipant:
    participant = match.participants.filter(player=player, is_active=True).first()
    if participant is None:
        raise InvalidAction("You are not in this match.")
    return participant
