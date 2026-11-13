from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import Interaction, app_commands

from bot.db import db
from bot.handlers.common import actor, community_of
from bot.handlers.results import process_report
from bot.renderers.results import leaderboard_text
from bot.routing import guarded, reply
from league.domain import steam as steam_ids
from league.domain.errors import InvalidAction, NotAllowed
from league.integrations import deadlock_api
from league.models import Community, MatchParticipant, Player
from league.services import boards, common, queue, steam

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
    tier = f" · {stats.tier}" if stats.tier else ""
    placement = f" · placement {stats.placement.removeprefix('P ')}" if stats.placement else ""
    lines = [
        f"**{stats.name}**: {stats.rating} ({rank}){tier}{placement}",
        f"{stats.games} games, {stats.wins} wins, {stats.losses} losses",
    ]
    if stats.mu is not None and stats.sigma is not None:
        lines.append(
            f"-# Skill {stats.mu:.1f}, uncertainty {stats.sigma:.1f}. "
            "The rating is skill minus 3 uncertainties, times 40."
        )
    if stats.mvps or stats.keys:
        lines.append(f"🏅 {stats.mvps} MVP · ⭐ {stats.keys} key player")
    if stats.avg_kda is not None:
        k, d, a = stats.avg_kda
        damage = f", {stats.avg_damage / 1000:.1f}k damage" if stats.avg_damage else ""
        lines.append(f"Scoreboards: {stats.scoreboards}, averaging {k:.1f}/{d:.1f}/{a:.1f}{damage}")
    lines.append(f"{stats.strikes} Captain: {stats.captain}.")
    lines.append(steam.describe(player))
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


@app_commands.command(
    name="stats", description="A player's rating, record, awards, strikes and recent changes"
)
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


rating_group = app_commands.Group(name="rating", description="Rating history and staff adjustments")


@rating_group.command(name="history", description="A player's recent rating changes")
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


# --- Steam -----------------------------------------------------------------------------------


class LinkConfirm(discord.ui.View):
    """Asks the player to check it's their account before linking it."""

    def __init__(self, player: Player, account_id: int, steam_name: str) -> None:
        super().__init__(timeout=120)
        self.player = player
        self.account_id = account_id
        self.steam_name = steam_name

    @discord.ui.button(label="Link it", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: Interaction[Any], _button: discord.ui.Button[Any]) -> None:
        self.stop()

        async def run() -> None:
            await interaction.response.edit_message(view=None)
            row = await db(steam.link, self.player, self.account_id, self.steam_name)
            await reply(interaction, f"Linked. {steam.describe(row)}")

        await guarded(interaction, run)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction[Any], _button: discord.ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content="Not linked.", view=None)


async def resolve_steam(text: str) -> int:
    """A 32-bit account ID from what the player pasted. Network only for custom URLs."""
    ref = steam_ids.parse(text)
    if ref is None:
        raise InvalidAction(
            "That isn't a Steam account I can read. Paste your profile link, SteamID64 or "
            "friend code (Steam: Friends, Add a Friend)."
        )
    if ref.account_id is not None:
        return ref.account_id
    try:
        steam64 = await asyncio.to_thread(deadlock_api.resolve_vanity, ref.vanity)
    except deadlock_api.ApiError as exc:
        raise InvalidAction("Steam didn't answer. Try your friend code instead.") from exc
    account = steam_ids.account_from_steam64(steam64) if steam64 else None
    if account is None:
        raise InvalidAction(
            "I can't look up custom profile links here. Paste your friend code instead "
            "(Steam: Friends, Add a Friend)."
        )
    return account


@app_commands.command(name="link", description="Link your Steam account (optional)")
@app_commands.describe(account="Profile link, SteamID64 or friend code")
async def link_command(interaction: Interaction[Any], account: str) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        player = await actor(interaction)
        account_id = await resolve_steam(account)
        try:
            names = await asyncio.to_thread(deadlock_api.steam_names, [account_id])
        except deadlock_api.ApiError:
            names = {}
        name = names.get(account_id, "")
        shown = f"**{name}**" if name else "an account deadlock-api doesn't know yet"
        text = (
            f"Link {shown} (account {account_id}) to you? It's checked against your matches "
            "once deadlock-api has one of them."
        )
        await interaction.followup.send(
            text, view=LinkConfirm(player, account_id, name), ephemeral=True
        )

    await guarded(interaction, run)


@app_commands.command(name="unlink", description="Remove your linked Steam account")
async def unlink_command(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        removed = await db(steam.unlink, await actor(interaction))
        await reply(interaction, "Unlinked." if removed else "You had no Steam account linked.")

    await guarded(interaction, run)


COMMANDS: tuple[Any, ...] = (
    leaderboard_command,
    stats_command,
    submit_command,
    link_command,
    unlink_command,
    queue_group,
    rating_group,
)
