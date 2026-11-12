"""The database side of the outbox: fetching due work and recording what happened."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from league.domain.engine import LIVE
from league.models import Community, Match, MatchParticipant, MatchResource, OutboxEffect
from league.models.matches import ResourceKind
from league.models.outbox import EffectStatus
from league.services.common import add_effect, now, render_board, render_queue

MAX_ATTEMPTS = 8
MAX_BACKOFF_SECONDS = 300

# A board that moves just as a captain clicks could cost them a wrong ban, so chat only moves it
# once it's being pushed out of view, and then waits for a quiet moment. Non-stop chat can only hold
# that off for so long. A message or two left below the board moves it much later, if nothing else
# has by then.
BOARD_MOVE_AFTER_MESSAGES = 3
BOARD_MOVE_QUIET_SECONDS = 5
BOARD_MOVE_MAX_WAIT_SECONDS = 20
BOARD_MOVE_IDLE_SECONDS = 3600


def due_by_match(limit: int = 200) -> list[list[OutboxEffect]]:
    """Due effects grouped so each match runs its effects in order.

    If an earlier effect of a match is waiting on a retry, later ones of that match wait too,
    unless they were created with a future `run_after`, such as the reminder and final cleanup,
    which are independent of the immediate work.
    """
    moment = now()
    retrying: dict[object, int] = {}
    waiting = OutboxEffect.objects.filter(status=EffectStatus.PENDING, attempts__gt=0).order_by(
        "id"
    )
    for match_id, effect_id in waiting.values_list("match_id", "id"):
        retrying.setdefault(match_id, effect_id)

    groups: dict[object, list[OutboxEffect]] = defaultdict(list)
    due = (
        OutboxEffect.objects.filter(status=EffectStatus.PENDING, run_after__lte=moment)
        .select_related("community", "match")
        .order_by("id")[:limit]
    )
    for effect in due:
        blocker = retrying.get(effect.match_id)
        if blocker is not None and effect.pk > blocker:
            continue
        groups[effect.match_id].append(effect)
    return list(groups.values())


def mark_started(effect_id: int) -> bool:
    """Claim an effect before running it. False if it's no longer pending, so it shouldn't run.

    A render that has started has already read its state, so requests from now on stop
    coalescing into it (see `request_render`).
    """
    claimed = OutboxEffect.objects.filter(pk=effect_id, status=EffectStatus.PENDING).update(
        started_at=now()
    )
    return claimed > 0


def mark_done(effect_id: int, status: str = EffectStatus.DONE) -> None:
    OutboxEffect.objects.filter(pk=effect_id).update(status=status, done_at=now(), last_error="")


def mark_retry(effect_id: int, error: str) -> str:
    """Back off and retry, or fail for good once attempts run out. Returns the new status."""
    effect = OutboxEffect.objects.get(pk=effect_id)
    effect.attempts += 1
    effect.last_error = error[:2000]
    if effect.attempts >= MAX_ATTEMPTS:
        effect.status = EffectStatus.FAILED
        effect.done_at = now()
    else:
        delay = min(2**effect.attempts, MAX_BACKOFF_SECONDS)
        effect.run_after = now() + timedelta(seconds=delay)
    effect.save(update_fields=["attempts", "last_error", "status", "done_at", "run_after"])
    return effect.status


def mark_failed(effect_id: int, error: str) -> None:
    OutboxEffect.objects.filter(pk=effect_id).update(
        status=EffectStatus.FAILED, last_error=error[:2000], done_at=now()
    )


def record_resource(match_id: int, kind: str, discord_id: int) -> None:
    MatchResource.objects.update_or_create(
        match_id=match_id, kind=kind, defaults={"discord_id": discord_id, "deleted_at": None}
    )


def resource_id(match_id: int, kind: str) -> int | None:
    return (
        MatchResource.objects.filter(match_id=match_id, kind=kind, deleted_at__isnull=True)
        .values_list("discord_id", flat=True)
        .first()
    )


def resources(match_id: int) -> dict[str, int]:
    rows = MatchResource.objects.filter(match_id=match_id, deleted_at__isnull=True).exclude(
        kind=ResourceKind.NOTICE
    )
    return dict(rows.values_list("kind", "discord_id"))


def mark_resource_deleted(match_id: int, kind: str) -> None:
    MatchResource.objects.filter(match_id=match_id, kind=kind).update(deleted_at=now())


def record_notice(match_id: int, message_id: int) -> None:
    MatchResource.objects.create(match_id=match_id, kind=ResourceKind.NOTICE, discord_id=message_id)


def live_notices(match_id: int) -> list[int]:
    rows = MatchResource.objects.filter(
        match_id=match_id, kind=ResourceKind.NOTICE, deleted_at__isnull=True
    )
    return list(rows.order_by("id").values_list("discord_id", flat=True))


def mark_notice_deleted(match_id: int, message_id: int) -> None:
    MatchResource.objects.filter(
        match_id=match_id, kind=ResourceKind.NOTICE, discord_id=message_id
    ).update(deleted_at=now())


def note_chat(channel_id: int) -> None:
    """Count a chat message in a match channel towards moving the board back below the chat."""
    resource = (
        MatchResource.objects.filter(
            kind=ResourceKind.TEXT, discord_id=channel_id, deleted_at__isnull=True
        )
        .select_related("match__community")
        .first()
    )
    if resource is None:
        return  # Any other channel in the server.
    match = resource.match
    moment = now()
    pending = OutboxEffect.objects.filter(
        match=match, kind="move_board", status=EffectStatus.PENDING, attempts=0
    ).first()
    if pending is None:
        add_effect(
            match.community,
            "move_board",
            {"messages": 1},
            match=match,
            run_after=moment + timedelta(seconds=BOARD_MOVE_IDLE_SECONDS),
        )
        return
    messages = int(pending.payload.get("messages", 0)) + 1
    pending.payload["messages"] = messages
    if messages >= BOARD_MOVE_AFTER_MESSAGES:
        latest = moment + timedelta(seconds=BOARD_MOVE_MAX_WAIT_SECONDS)
        move_by = datetime.fromisoformat(pending.payload.setdefault("move_by", latest.isoformat()))
        pending.run_after = min(moment + timedelta(seconds=BOARD_MOVE_QUIET_SECONDS), move_by)
    pending.save(update_fields=["payload", "run_after"])


def cancel_board_moves(match_id: int) -> None:
    """The board has just moved to the bottom, so chat before now no longer counts."""
    OutboxEffect.objects.filter(
        match_id=match_id, kind="move_board", status=EffectStatus.PENDING, attempts=0
    ).update(status=EffectStatus.SKIPPED, done_at=now())


def voice_target(discord_user_id: int, joined_channel_id: int) -> tuple[int, int] | None:
    """Where a player joining this channel should be sent: (guild ID, channel ID), or None.

    Covers someone who joins the Waiting Room after their lobby popped, or the lobby voice after
    teams moved on. Anyone else is left alone, wherever they are.
    """
    participant = (
        MatchParticipant.objects.filter(player__discord_user_id=discord_user_id, is_active=True)
        .select_related("match__community")
        .first()
    )
    if participant is None:
        return None
    match = participant.match
    channels = resources(match.pk)
    team_kind = {"A": ResourceKind.TEAM_A_VOICE, "B": ResourceKind.TEAM_B_VOICE}.get(
        participant.team
    )
    team_channel = channels.get(team_kind) if team_kind else None
    lobby_channel = channels.get(ResourceKind.LOBBY_VOICE)
    target = team_channel or lobby_channel
    if target is None or target == joined_channel_id:
        return None
    if joined_channel_id not in {match.community.waiting_room_channel_id, lobby_channel}:
        return None
    return match.community.discord_guild_id, target


def stale_resources() -> list[tuple[int, str, int]]:
    """Channels of finished matches whose delete effect has failed for good."""
    finished = Match.objects.filter(status__in=("completed", "voided"), ended_at__isnull=False)
    failed = OutboxEffect.objects.filter(
        match__in=finished,
        kind__in=("cleanup_voice", "delete_text_channel"),
        status=EffectStatus.FAILED,
    ).values_list("match_id", flat=True)
    rows = MatchResource.objects.filter(
        match_id__in=failed,
        deleted_at__isnull=True,
        kind__in=("text", "lobby_voice", "team_a_voice", "team_b_voice"),
    )
    return list(rows.values_list("match_id", "kind", "discord_id"))


def request_reconcile() -> int:
    """After a restart: re-render everything live, in case Discord missed an update."""
    count = 0
    for community in Community.objects.all():
        render_queue(community)
        count += 1
    live = Match.objects.filter(status__in=LIVE).select_related("community")
    for match in live:
        render_board(match)
        count += 1
    return count


def prune_finished(older_than_days: int = 30) -> int:
    """Done effects are operational data, not history, so they don't need keeping for ever."""
    cutoff = now() - timedelta(days=older_than_days)
    deleted, _ = OutboxEffect.objects.filter(
        status__in=(EffectStatus.DONE, EffectStatus.SKIPPED), done_at__lt=cutoff
    ).delete()
    return deleted
