"""What each outbox effect does. Each handler is safe to run again after a crash or retry."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import discord

from bot.db import db
from bot.effects.gateway import ChannelGone, Gateway
from bot.renderers.board import (
    render_board,
    render_pickers,
    render_ready_dm,
    render_sub_offer_dm,
    render_voice_dm,
)
from bot.renderers.prio import render_prio
from bot.renderers.queue import render_queue
from bot.renderers.quickdraft import hero_grid, render_quick_draft
from bot.renderers.results import render_correction, render_results
from league.domain.engine import Status
from league.domain.errors import DomainError
from league.models import Community, Match, OutboxEffect, Player
from league.models.matches import ResourceKind
from league.services import boards, dev, effects, lobby, prio, quickdraft, subs
from league.services.common import add_effect
from league.services.common import render_board as request_board

logger = logging.getLogger(__name__)

# Long enough to outlast a ready check and its backfills.
INVITE_MAX_AGE_SECONDS = 1800


class Skip(Exception):
    """Nothing to do, and that's fine: for example, a fake player has no Discord account."""


@dataclass(frozen=True, slots=True)
class Env:
    gateway: Gateway
    effect: OutboxEffect
    community: Community
    match: Match | None

    @property
    def guild_id(self) -> int:
        return self.community.discord_guild_id

    @property
    def required_match(self) -> Match:
        if self.match is None:
            raise ValueError(f"{self.effect.kind} needs a match")
        return self.match

    @property
    def match_id(self) -> int:
        return self.required_match.pk


Handler = Callable[[Env], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def effect(kind: str) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        HANDLERS[kind] = handler
        return handler

    return register


def _real(user_ids: list[int]) -> list[int]:
    return [user_id for user_id in user_ids if not dev.is_fake(user_id)]


def _active_user_ids(match: Match) -> list[int]:
    return list(
        match.participants.filter(is_active=True).values_list("player__discord_user_id", flat=True)
    )


def _team_user_ids(match: Match, team: str) -> list[int]:
    return list(
        match.participants.filter(team=team, removed_reason="").values_list(
            "player__discord_user_id", flat=True
        )
    )


async def _text_channel(env: Env) -> int:
    channel_id = await db(effects.resource_id, env.match_id, ResourceKind.TEXT)
    if channel_id is None:
        raise Skip("No lobby text channel.")
    return channel_id


def _reload(match_id: int) -> Match:
    return Match.objects.select_related("community").get(pk=match_id)


async def _notice(
    env: Env, text: str, *, user_ids: Iterable[int] = (), role_id: int | None = None
) -> None:
    """Post a message about the flow. It stays until the match moves on, with the board below it.

    The board moves in its own effect, so a failed move can't retry this and ping people twice.
    """
    message_id = await env.gateway.send_text(
        await _text_channel(env), text, user_ids=user_ids, role_id=role_id
    )
    await db(effects.record_notice, env.match_id, message_id)
    await db(_request_board, env.match_id)


def _request_board(match_id: int) -> None:
    request_board(_reload(match_id))


# --- Lobby -----------------------------------------------------------------------------------


@effect("create_lobby")
async def create_lobby(env: Env) -> None:
    existing = await db(effects.resources, env.match_id)
    members = _real(await db(_active_user_ids, env.required_match))
    community = env.community
    if ResourceKind.TEXT not in existing:
        text_id = await env.gateway.create_text_channel(
            env.guild_id,
            community.match_category_id,
            f"match-{env.match_id}",
            members,
            community.staff_role_id,
        )
        await db(effects.record_resource, env.match_id, ResourceKind.TEXT, text_id)
    if ResourceKind.LOBBY_VOICE not in existing:
        voice_id = await env.gateway.create_voice_channel(
            env.guild_id,
            community.match_category_id,
            f"Match {env.match_id} Lobby",
            members,
            community.staff_role_id,
        )
        await db(effects.record_resource, env.match_id, ResourceKind.LOBBY_VOICE, voice_id)
    await _notice(env, "your lobby is ready. Press **Ready** on the board below.", user_ids=members)


def channel_url(guild_id: int, channel_id: int | None) -> str | None:
    """A link that opens the channel, for DMs where a <#channel> mention may not resolve."""
    return f"https://discord.com/channels/{guild_id}/{channel_id}" if channel_id else None


@effect("gather_player")
async def gather_player(env: Env) -> None:
    """In the Waiting Room: move them. Anywhere else: DM an invite. They choose when to join."""
    user_id = int(env.effect.payload["discord_user_id"])
    is_sub = bool(env.effect.payload.get("sub"))
    if dev.is_fake(user_id):
        if not is_sub:
            await db(_auto_ready, env.match_id, int(env.effect.payload["player_id"]))
        raise Skip("Fake player")
    resources = await db(effects.resources, env.match_id)
    link = channel_url(env.guild_id, resources.get(ResourceKind.TEXT))
    if is_sub:
        await _gather_sub(env, user_id, resources, link)
        return
    lobby_voice = resources.get(ResourceKind.LOBBY_VOICE)
    if lobby_voice is None:
        raise RuntimeError("Lobby voice channel isn't recorded yet.")
    waiting = env.gateway.voice_member_ids(env.guild_id, env.community.waiting_room_channel_id)
    if user_id in waiting and await env.gateway.move_member(env.guild_id, user_id, lobby_voice):
        return
    invite = await env.gateway.create_invite(env.guild_id, lobby_voice, INVITE_MAX_AGE_SECONDS)
    view = render_ready_dm(env.match_id, invite, heading="Your match is ready", channel_url=link)
    if not await env.gateway.send_dm(user_id, view):
        await _notice(
            env,
            "I couldn't DM you. Join the lobby voice channel and press Ready on the board.",
            user_ids=[user_id],
        )


async def _gather_sub(env: Env, user_id: int, resources: dict[str, int], link: str | None) -> None:
    """A sub joins wherever the match is now: their team's voice once teams are set."""
    team = await db(_team_of, env.match_id, user_id)
    target = resources.get(_team_voice_kind(team)) or resources.get(ResourceKind.LOBBY_VOICE)
    if target is not None:
        nearby = (env.community.waiting_room_channel_id, resources.get(ResourceKind.LOBBY_VOICE))
        here = any(
            user_id in env.gateway.voice_member_ids(env.guild_id, channel)
            for channel in nearby
            if channel and channel != target
        )
        if here and await env.gateway.move_member(env.guild_id, user_id, target):
            return
    invite = (
        await env.gateway.create_invite(env.guild_id, target, INVITE_MAX_AGE_SECONDS)
        if target is not None
        else None
    )
    if await db(_needs_ready, env.match_id, user_id):
        view = render_ready_dm(env.match_id, invite, heading="You're subbing in", channel_url=link)
    else:
        view = render_voice_dm(
            env.match_id,
            invite,
            heading="You're subbing in",
            body="Join voice and keep an eye on the board.",
            channel_url=link,
            voice_label="Join voice",
        )
    if not await env.gateway.send_dm(user_id, view):
        await _notice(env, "you're in. Join voice and check the board.", user_ids=[user_id])


def _needs_ready(match_id: int, user_id: int) -> bool:
    match = Match.objects.get(pk=match_id)
    return (
        match.status == Status.READY_CHECK
        and match.participants.filter(
            player__discord_user_id=user_id, is_active=True, ready_at__isnull=True
        ).exists()
    )


@effect("sub_offer")
async def sub_offer(env: Env) -> None:
    """DM the offer of an open spot. If they can't be reached, the next person gets asked."""
    offer_id = int(env.effect.payload["offer_id"])
    info = await db(subs.offer_info, offer_id)
    if not info.pending:
        raise Skip("The offer has closed.")
    if dev.is_fake(info.discord_user_id):
        await db(_auto_accept_offer, offer_id, info.player_id)
        raise Skip("Fake player")
    team = await db(_team_label, env.match_id, info.team)
    if not await env.gateway.send_dm(info.discord_user_id, render_sub_offer_dm(info, team)):
        await db(subs.fail_offer, offer_id)


def _team_label(match_id: int, team: str) -> str:
    captains = boards.captain_names(Match.objects.get(pk=match_id))
    return boards.team_name(captains[team]) if team in captains else ""


def _auto_accept_offer(offer_id: int, player_id: int) -> None:
    """Smoke tests only: a fake player in the queue takes any spot offered to them."""
    try:
        subs.answer_offer(offer_id, Player.objects.get(pk=player_id), True)
    except DomainError:
        logger.debug("Fake player could not sub in", extra={"offer": offer_id})


def _auto_ready(match_id: int, player_id: int) -> None:
    """Smoke tests only: fake players can't click, so they ready up as soon as they're gathered."""
    try:
        lobby.mark_ready(match_id, Player.objects.get(pk=player_id))
    except DomainError:
        logger.debug("Fake player could not ready", extra={"match": match_id})


@effect("grant_access")
async def grant_access(env: Env) -> None:
    user_id = int(env.effect.payload["discord_user_id"])
    if dev.is_fake(user_id):
        raise Skip("Fake player")
    resources = await db(effects.resources, env.match_id)
    team = await db(_team_of, env.match_id, user_id)
    kinds = (ResourceKind.TEXT, ResourceKind.LOBBY_VOICE, _team_voice_kind(team))
    for kind in kinds:
        channel_id = resources.get(kind)
        if channel_id:
            await env.gateway.grant_member(channel_id, user_id)


def _team_of(match_id: int, user_id: int) -> str:
    participant = (
        Match.objects.get(pk=match_id)
        .participants.filter(player__discord_user_id=user_id, is_active=True)
        .first()
    )
    return participant.team if participant else ""


def _team_voice_kind(team: str) -> str:
    return {"A": ResourceKind.TEAM_A_VOICE, "B": ResourceKind.TEAM_B_VOICE}.get(team, "")


@effect("remove_player")
async def remove_player(env: Env) -> None:
    user_id = int(env.effect.payload["discord_user_id"])
    if dev.is_fake(user_id):
        raise Skip("Fake player")
    resources = await db(effects.resources, env.match_id)
    voice_kinds = {
        ResourceKind.LOBBY_VOICE,
        ResourceKind.TEAM_A_VOICE,
        ResourceKind.TEAM_B_VOICE,
    }
    waiting = env.community.waiting_room_channel_id
    for kind, discord_id in resources.items():
        if (
            kind in voice_kinds
            and user_id in env.gateway.voice_member_ids(env.guild_id, discord_id)
            and waiting
        ):
            await env.gateway.move_member(env.guild_id, user_id, waiting)
        if kind in voice_kinds or kind == ResourceKind.TEXT:
            await env.gateway.revoke_member(discord_id, user_id)


# --- Boards ----------------------------------------------------------------------------------


# The champion grid's messages, top to bottom. They sit directly below the board.
GRID_KINDS = (ResourceKind.PICKER_MESSAGE, ResourceKind.PICKER_MESSAGE_2)


def _last_message(resources: dict[str, int]) -> int | None:
    """The bottom of the flow: the last grid message, or the board when there is no grid."""
    for kind in reversed(GRID_KINDS):
        if kind in resources:
            return resources[kind]
    return resources.get(ResourceKind.BOARD_MESSAGE)


async def _render_board(env: Env) -> None:
    resources = await db(effects.resources, env.match_id)
    text_id = resources.get(ResourceKind.TEXT)
    if text_id is None:
        raise Skip("Lobby channel isn't up yet; creating it renders the board.")
    state = await db(boards.board_state, env.match_id)
    board = render_board(state)
    grid = render_pickers(state)
    board_id = resources.get(ResourceKind.BOARD_MESSAGE)
    in_place = False
    if board_id is not None and await env.gateway.latest_message_id(text_id) == _last_message(
        resources
    ):
        try:
            await env.gateway.edit_view(text_id, board_id, board)
        except ChannelGone:
            logger.info("The board was deleted; posting it again", extra={"match": env.match_id})
        else:
            in_place = True
    if not in_place:
        # Post before deleting, so the flow is never missing. Old buttons stay safe in between:
        # they carry their step, and a stale click gets a polite refusal. Each old message goes
        # as soon as its replacement is recorded, so a retry can't leave one behind.
        message_id = await env.gateway.send_view(text_id, board)
        await db(effects.record_resource, env.match_id, ResourceKind.BOARD_MESSAGE, message_id)
        if board_id is not None:
            await env.gateway.delete_message(text_id, board_id)
    await _repost_grid(env, text_id, resources, grid)
    if not in_place:
        await db(effects.cancel_board_moves, env.match_id)  # Chat now starts counting afresh.


async def _repost_grid(env: Env, text_id: int, resources: dict[str, int], grid: list[Any]) -> None:
    """Post the champion grid afresh below the board, then remove the old one.

    The grid is never edited: an edited message gets an "(edited)" tag, which would split it.
    Each old message goes as soon as its replacement is recorded, so a retry can't leave one
    behind, and the new messages end up in order directly below the board.
    """
    for index, kind in enumerate(GRID_KINDS):
        old_id = resources.get(kind)
        if index < len(grid):
            message_id = await env.gateway.send_view(text_id, grid[index])
            await db(effects.record_resource, env.match_id, kind, message_id)
        elif old_id is not None:
            await db(effects.mark_resource_deleted, env.match_id, kind)
        if old_id is not None:
            await env.gateway.delete_message(text_id, old_id)


@effect("render_board")
async def render_board_effect(env: Env) -> None:
    await _render_board(env)


@effect("move_board")
async def move_board(env: Env) -> None:
    """Chat has built up below the board, unless a state change has already moved it since."""
    resources = await db(effects.resources, env.match_id)
    text_id = resources.get(ResourceKind.TEXT)
    if text_id is None:
        raise Skip("The channel has closed.")
    last_id = _last_message(resources)
    if last_id is not None and await env.gateway.latest_message_id(text_id) == last_id:
        raise Skip("The board is already last.")
    await _render_board(env)


@effect("clear_notices")
async def clear_notices(env: Env) -> None:
    text_id = await db(effects.resource_id, env.match_id, ResourceKind.TEXT)
    if text_id is None:
        raise Skip("The channel has closed.")
    for message_id in await db(effects.live_notices, env.match_id):
        await env.gateway.delete_message(text_id, message_id)
        await db(effects.mark_notice_deleted, env.match_id, message_id)


@effect("render_queue")
async def render_queue_effect(env: Env) -> None:
    community = env.community
    if not community.queue_channel_id:
        raise Skip("Queue channel isn't set up.")
    view = render_queue(await db(boards.queue_view, community))
    if community.queue_message_id:
        try:
            await env.gateway.edit_view(
                community.queue_channel_id, community.queue_message_id, view
            )
            return
        except ChannelGone:
            logger.warning("Queue message is gone; posting a new one")
    from league.services import community as community_service

    message_id = await env.gateway.send_view(community.queue_channel_id, view)
    await db(community_service.save_wiring, community.pk, queue_message_id=message_id)


# --- Messages --------------------------------------------------------------------------------


@effect("ping")
async def ping(env: Env) -> None:
    await _notice(
        env,
        str(env.effect.payload["text"]),
        user_ids=_real([int(uid) for uid in env.effect.payload["user_ids"]]),
    )


@effect("ping_lobby")
async def ping_lobby(env: Env) -> None:
    await _notice(env, str(env.effect.payload["text"]))


@effect("ping_staff")
async def ping_staff(env: Env) -> None:
    await _notice(
        env,
        f"Match #{env.match_id} {env.effect.payload['text']}",
        role_id=env.community.staff_role_id,
    )


@effect("announce_lobby_code")
async def announce_lobby_code(env: Env) -> None:
    match = await db(_reload, env.match_id)
    members = _real(await db(_active_user_ids, match))
    await _notice(env, f"Lobby code: `{match.lobby_code}`", user_ids=members)
    resources = await db(effects.resources, env.match_id)
    for kind in (ResourceKind.TEAM_A_VOICE, ResourceKind.TEAM_B_VOICE):
        channel_id = resources.get(kind)
        if channel_id:
            await env.gateway.set_voice_status(channel_id, f"Lobby code: {match.lobby_code}")


@effect("report_reminder")
async def report_reminder(env: Env) -> None:
    match = await db(_reload, env.match_id)
    if match.status != Status.IN_PROGRESS:
        raise Skip("The result is already in.")
    captains = await db(_captain_ids, match)
    await _notice(
        env,
        "if your game has ended, press the team that won on the board and add the scoreboard.",
        user_ids=_real(captains),
    )


def _captain_ids(match: Match) -> list[int]:
    return list(
        match.participants.filter(is_captain=True, is_active=True).values_list(
            "player__discord_user_id", flat=True
        )
    )


# --- Team voice ------------------------------------------------------------------------------


def _enqueue_team_moves(match_id: int, user_ids: list[tuple[int, str]]) -> None:
    match = Match.objects.select_related("community").get(pk=match_id)
    for user_id, team in user_ids:
        add_effect(
            match.community,
            "team_move_player",
            {"discord_user_id": user_id, "team": team},
            match=match,
            dedupe_key=f"team_move:{match_id}:{user_id}",
        )
    add_effect(
        match.community,
        "delete_lobby_voice",
        match=match,
        dedupe_key=f"delete_lobby_voice:{match_id}",
    )


@effect("create_team_voice")
async def create_team_voice(env: Env) -> None:
    resources = await db(effects.resources, env.match_id)
    community = env.community
    assignments: list[tuple[int, str]] = []
    captains = await db(boards.captain_names, env.required_match)
    for team, kind in (("A", ResourceKind.TEAM_A_VOICE), ("B", ResourceKind.TEAM_B_VOICE)):
        members = await db(_team_user_ids, env.required_match, team)
        assignments.extend((user_id, team) for user_id in members)
        if kind in resources:
            continue
        channel_id = await env.gateway.create_voice_channel(
            env.guild_id,
            community.match_category_id,
            boards.team_name(captains.get(team, f"Match {env.match_id}"))[:100],
            _real(members),
            community.staff_role_id,
        )
        await db(effects.record_resource, env.match_id, kind, channel_id)
    await db(_enqueue_team_moves, env.match_id, assignments)


@effect("team_move_player")
async def team_move_player(env: Env) -> None:
    """In the lobby VC: move them. Anywhere else: DM an invite to their team's channel."""
    user_id = int(env.effect.payload["discord_user_id"])
    if dev.is_fake(user_id):
        raise Skip("Fake player")
    resources = await db(effects.resources, env.match_id)
    team_channel = resources.get(_team_voice_kind(str(env.effect.payload["team"])))
    if team_channel is None:
        raise Skip("Team channel is gone.")
    lobby_voice = resources.get(ResourceKind.LOBBY_VOICE)
    in_lobby = user_id in env.gateway.voice_member_ids(env.guild_id, lobby_voice)
    if in_lobby and await env.gateway.move_member(env.guild_id, user_id, team_channel):
        return
    invite = await env.gateway.create_invite(env.guild_id, team_channel, INVITE_MAX_AGE_SECONDS)
    view = render_voice_dm(
        env.match_id,
        invite,
        heading="Teams are set: join your team's voice",
        channel_url=channel_url(env.guild_id, resources.get(ResourceKind.TEXT)),
    )
    if not await env.gateway.send_dm(user_id, view):
        await _notice(env, "teams are set. Join your team's voice channel.", user_ids=[user_id])


@effect("delete_lobby_voice")
async def delete_lobby_voice(env: Env) -> None:
    channel_id = await db(effects.resource_id, env.match_id, ResourceKind.LOBBY_VOICE)
    if channel_id is None:
        raise Skip("Already deleted.")
    await env.gateway.delete_channel(channel_id)
    await db(effects.mark_resource_deleted, env.match_id, ResourceKind.LOBBY_VOICE)


# --- Results ---------------------------------------------------------------------------------


@effect("post_results")
async def post_results(env: Env) -> None:
    results_channel = env.community.results_channel_id
    if not results_channel:
        raise Skip("Results channel isn't set up.")
    if await db(effects.resource_id, env.match_id, ResourceKind.RESULTS_MESSAGE):
        raise Skip("Already posted.")  # A retry after a crash must not post twice.
    view_model = await db(boards.results_view, env.match_id)
    file = None
    shot = view_model.screenshot
    if shot is not None and shot.archive_channel_id and shot.archive_message_id:
        fetched = await env.gateway.fetch_attachment(
            shot.archive_channel_id, shot.archive_message_id, shot.attachment_id or 0
        )
        if fetched is not None:
            suffix = fetched[0].rsplit(".", 1)[-1] if "." in fetched[0] else "png"
            file = (f"match-{env.match_id}.{suffix}", fetched[1])
    view = render_results(view_model, attachment_name=file[0] if file else None)
    message_id = await env.gateway.send_view(results_channel, view, file=file)
    await db(effects.record_resource, env.match_id, ResourceKind.RESULTS_MESSAGE, message_id)


@effect("post_correction")
async def post_correction(env: Env) -> None:
    results_channel = env.community.results_channel_id
    if not results_channel:
        raise Skip("Results channel isn't set up.")
    run_id = env.effect.payload.get("run_id")
    view_model = await db(
        boards.correction_view,
        env.match_id,
        int(run_id) if run_id else None,
        str(env.effect.payload.get("outcome", "")),
        str(env.effect.payload.get("reason", "")),
    )
    await env.gateway.send_view(results_channel, render_correction(view_model))


# --- Cleanup ---------------------------------------------------------------------------------


@effect("cleanup_voice")
async def cleanup_voice(env: Env) -> None:
    """The match is over: everyone in it goes back to the Waiting Room, wherever they are in
    voice, and then its voice channels go."""
    resources = await db(effects.resources, env.match_id)
    waiting = env.community.waiting_room_channel_id
    if waiting:
        await _send_players_back(env, waiting)
    for kind in (ResourceKind.LOBBY_VOICE, ResourceKind.TEAM_A_VOICE, ResourceKind.TEAM_B_VOICE):
        channel_id = resources.get(kind)
        if channel_id is None:
            continue
        if waiting:
            for user_id in env.gateway.voice_member_ids(env.guild_id, channel_id):
                await env.gateway.move_member(env.guild_id, user_id, waiting)
        await env.gateway.delete_channel(channel_id)
        await db(effects.mark_resource_deleted, env.match_id, kind)
    if waiting:
        # Anyone who was already in the Waiting Room gets no voice event to put them back on
        # the priority list, so catch the list up now.
        here = env.gateway.voice_member_ids(env.guild_id, waiting)
        await db(prio.reconcile, env.community.pk, dict.fromkeys(here, ""))


async def _send_players_back(env: Env, waiting: int) -> None:
    already = env.gateway.voice_member_ids(env.guild_id, waiting)
    for user_id in await db(effects.players_to_send_back, env.match_id):
        if user_id in already or dev.is_fake(user_id):
            continue
        try:
            # False just means they aren't in voice.
            await env.gateway.move_member(env.guild_id, user_id, waiting)
        except discord.HTTPException as exc:
            # One member we can't move mustn't stop the channels being cleaned up.
            logger.warning(
                "Couldn't move player back to the Waiting Room",
                extra={"match": env.match_id, "user": user_id, "status": exc.status},
            )


@effect("delete_text_channel")
async def delete_text_channel(env: Env) -> None:
    channel_id = await db(effects.resource_id, env.match_id, ResourceKind.TEXT)
    if channel_id is None:
        raise Skip("Already deleted.")
    await env.gateway.delete_channel(channel_id)
    await db(effects.mark_resource_deleted, env.match_id, ResourceKind.TEXT)


# --- Standalone drafts (/startdraft) ----------------------------------------------------------


@effect("render_quick_draft")
async def render_quick_draft_effect(env: Env) -> None:
    """The match board's approach: the draft message, with the hero grid in messages of its own
    directly below. The draft message is edited in place while it's still at the bottom, and
    posted again below any chat otherwise. The grid is posted afresh each turn."""
    draft_id = int(env.effect.payload["draft_id"])
    state = await db(quickdraft.state, draft_id)
    if state.message_id is None or state.status == "stopped":
        raise Skip("The draft has no message to edit.")
    channel_id = state.channel_id
    view = render_quick_draft(state)
    bottom = state.grid_message_ids[-1] if state.grid_message_ids else state.message_id
    try:
        try:
            at_bottom = await env.gateway.latest_message_id(channel_id) == bottom
        except discord.Forbidden:
            at_bottom = True  # Can't read the channel's history: just keep editing in place.
        if at_bottom:
            await env.gateway.edit_view(channel_id, state.message_id, view)
        else:
            message_id = await env.gateway.send_view(channel_id, view)
            await db(quickdraft.moved_message, draft_id, message_id)
            await env.gateway.delete_message(channel_id, state.message_id)
    except ChannelGone:
        # Deleting the draft message (or the channel) stops the draft, as it did in Bebot.
        await db(quickdraft.message_gone, draft_id)
        raise
    await _repost_hero_grid(env, state, hero_grid(state))


async def _repost_hero_grid(env: Env, state: Any, grid: list[Any]) -> None:
    """Post the new grid, then remove the old one. Each step is recorded as it happens, so a
    retry never leaves a grid message behind."""
    ids: list[int | None] = list(state.grid_message_ids)
    for index in range(max(len(grid), len(ids))):
        old_id = ids[index] if index < len(ids) else None
        new_id = (
            await env.gateway.send_view(state.channel_id, grid[index])
            if index < len(grid)
            else None
        )
        if index < len(ids):
            ids[index] = new_id
        else:
            ids.append(new_id)
        await db(quickdraft.record_grid, state.id, [i for i in ids if i is not None])
        if old_id is not None:
            await env.gateway.delete_message(state.channel_id, old_id)


@effect("delete_quick_draft")
async def delete_quick_draft(env: Env) -> None:
    payload = env.effect.payload
    message_ids = payload.get("message_ids") or [payload["message_id"]]
    for message_id in message_ids:
        await env.gateway.delete_message(int(payload["channel_id"]), int(message_id))


# --- Voice priority list (/priolist) ---------------------------------------------------------


@effect("render_prio")
async def render_prio_effect(env: Env) -> None:
    community = await db(_reload_community, env.community.pk)
    if not community.prio_channel_id:
        raise Skip("No priority list display is set up.")
    view = render_prio(await db(prio.prio_view, community.pk))
    if community.prio_message_id:
        try:
            await env.gateway.edit_view(community.prio_channel_id, community.prio_message_id, view)
            return
        except ChannelGone:
            logger.warning("Priority list message is gone; posting a new one")
    message_id = await env.gateway.send_view(community.prio_channel_id, view)
    await db(prio.save_display, community.pk, community.prio_channel_id, message_id)


def _reload_community(community_id: int) -> Community:
    return Community.objects.get(pk=community_id)
