from __future__ import annotations

from typing import Any

import discord
from discord import Interaction, app_commands

from bot.db import db
from bot.handlers.common import actor, community_of
from bot.handlers.results import process_report
from bot.renderers.results import leaderboard_text
from bot.routing import guarded, reply
from league.domain.errors import NotAllowed
from league.models import Community, MatchParticipant, Player
from league.services import boards, common, queue

TeamChoice = app_commands.Choice[str]
# Relative to the captain submitting, because a fixed list can't carry captains' names.
TEAM_CHOICES = [
    TeamChoice(name="My team", value="mine"),
    TeamChoice(name="The other team", value="other"),
]


def _leaderboard(community: Community) -> str:
    season = common.open_season(community)
    return leaderboard_text(boards.leaderboard(season), season.leaderboard_min_games)


def _stats_text(community: Community, player: Player) -> str:
    stats = boards.player_stats(common.open_season(community), player)
    rank = f"#{stats.rank}" if stats.rank else "unranked"
    lines = [
        f"**{stats.name}**: {stats.rating} ({rank})",
        f"{stats.games} games, {stats.wins} wins, {stats.losses} losses",
    ]
    if stats.recent:
        rows = [
            f"{'match ' + str(match_id) if match_id else 'staff':<12} {delta:+4d}  {after}"
            for match_id, delta, after in stats.recent
        ]
        lines.append("```\n" + "\n".join(rows) + "\n```")
    return "\n".join(lines)


def _queue_text(community: Community, player: Player) -> str:
    status = queue.status(community, player)
    if status.match_id is not None:
        return f"You're in match #{status.match_id}. {status.size} waiting in the queue."
    if status.position is not None:
        return f"You're #{status.position} of {status.size} in the queue."
    return f"{status.size} waiting in the queue. You're not in it."


def _live_captain_match(player: Player) -> tuple[int, str]:
    participant = MatchParticipant.objects.filter(
        player=player, is_active=True, is_captain=True
    ).first()
    if participant is None:
        raise NotAllowed("You're not a captain in a live match.")
    return participant.match_id, participant.team


@app_commands.command(name="leaderboard", description="Top players this season")
async def leaderboard_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        community = await community_of(interaction)
        await reply(interaction, await db(_leaderboard, community))

    await guarded(interaction, run)


async def _resolve_target(interaction: Interaction[Any], user: discord.abc.User | None) -> Player:
    if user is None:
        return await actor(interaction)
    return await db(common.get_or_create_player, user.id, user.display_name)


@app_commands.command(name="stats", description="A player's Elo, record and recent changes")
@app_commands.describe(player="Leave empty for yourself")
async def stats_command(
    interaction: Interaction[Any], player: discord.Member | None = None
) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        community = await community_of(interaction)
        target = await _resolve_target(interaction, player)
        await reply(interaction, await db(_stats_text, community, target))

    await guarded(interaction, run)


@app_commands.command(
    name="submit", description="Report a result: use this if the board's Report button fails"
)
@app_commands.describe(
    winner="Which team won",
    screenshot="The post-match scoreboard",
    deadlock_match_id="Optional Deadlock match ID",
)
@app_commands.choices(winner=TEAM_CHOICES)
async def submit_command(
    interaction: Interaction[Any],
    winner: TeamChoice,
    screenshot: discord.Attachment,
    deadlock_match_id: str = "",
) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = await actor(interaction)
        match_id, my_team = await db(_live_captain_match, player)
        other_team = "B" if my_team == "A" else "A"
        winning_team = my_team if winner.value == "mine" else other_team
        await process_report(
            interaction, match_id, player, winning_team, screenshot, deadlock_match_id
        )

    await guarded(interaction, run)


queue_group = app_commands.Group(name="queue", description="The match queue")


@queue_group.command(name="status", description="How long the queue is, and your place in it")
async def queue_status(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        community = await community_of(interaction)
        await reply(interaction, await db(_queue_text, community, await actor(interaction)))

    await guarded(interaction, run)


rating_group = app_commands.Group(name="rating", description="Elo history and staff adjustments")


@rating_group.command(name="history", description="A player's recent Elo changes")
@app_commands.describe(player="Leave empty for yourself")
async def rating_history(
    interaction: Interaction[Any], player: discord.Member | None = None
) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        community = await community_of(interaction)
        target = await _resolve_target(interaction, player)
        await reply(interaction, await db(_stats_text, community, target))

    await guarded(interaction, run)
