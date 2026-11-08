"""Staff commands. Anything that rewrites ratings shows a preview and needs a Confirm click."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import Interaction, app_commands

from bot.commands.player import queue_group, rating_group
from bot.db import db
from bot.handlers.common import actor, community_of, require_staff
from bot.routing import guarded, reply
from league.domain.engine import TERMINAL, Status
from league.domain.errors import InvalidAction
from league.domain.mode_schema import ModeValidationError
from league.models import Community, DraftModeVersion, Match, Player, RatingEvent
from league.services import (
    boards,
    champions,
    common,
    corrections,
    integrity,
    lobby,
    modes,
    penalties,
    queue,
    ratings,
    results,
)
from league.services.corrections import Preview


class ConfirmView(discord.ui.View):
    """A short-lived ephemeral confirmation. It doesn't need to survive a restart."""

    def __init__(self, on_confirm: Callable[[Interaction[Any]], Awaitable[None]]) -> None:
        super().__init__(timeout=120)
        self.on_confirm = on_confirm

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: Interaction[Any], _button: discord.ui.Button[Any]) -> None:
        self.stop()

        async def run() -> None:
            await interaction.response.edit_message(view=None)
            await self.on_confirm(interaction)

        await guarded(interaction, run)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction[Any], _button: discord.ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


async def _staff_start(interaction: Interaction[Any]) -> Community:
    await interaction.response.defer(ephemeral=True, thinking=True)
    community = await community_of(interaction)
    await require_staff(interaction, community)
    return community


def _get_match(community: Community, match_id: int) -> Match:
    match = (
        Match.objects.select_related("season", "community", "mode_version")
        .filter(pk=match_id, community=community)
        .first()
    )
    if match is None:
        raise InvalidAction(f"There's no match #{match_id}.")
    return match


def _preview_text(preview: Preview) -> str:
    if not preview.net_by_player:
        return "Nobody's rating changes."
    names = dict(
        Player.objects.filter(pk__in=preview.net_by_player).values_list("pk", "display_name")
    )
    ordered = sorted(preview.net_by_player.items(), key=lambda item: -abs(item[1]))
    rows = [f"{names[player_id][:16]:<16} {delta:+d}" for player_id, delta in ordered[:25]]
    extra = f"\n...and {len(ordered) - 25} more" if len(ordered) > 25 else ""
    return (
        f"This writes {preview.events_to_write} ledger rows, because later matches shift too.\n"
        "```\n" + "\n".join(rows) + extra + "\n```"
    )


async def _send_confirm(
    interaction: Interaction[Any],
    text: str,
    on_confirm: Callable[[Interaction[Any]], Awaitable[None]],
) -> None:
    await interaction.followup.send(text, view=ConfirmView(on_confirm), ephemeral=True)


match_group = app_commands.Group(name="match", description="Staff: manage matches")


@match_group.command(name="status", description="Where a match is up to")
async def match_status(interaction: Interaction[Any], match_id: int) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        match = await db(_get_match, community, match_id)
        deadline = (
            f", next timeout <t:{int(match.deadline_at.timestamp())}:R>"
            if match.deadline_at
            else ""
        )
        await reply(
            interaction,
            f"Match #{match.pk}: **{match.status}**{deadline}. Winner: {match.winner or '-'}.",
        )

    await guarded(interaction, run)


@match_group.command(name="void", description="Cancel a live match, or void a finished one")
async def match_void(interaction: Interaction[Any], match_id: int, reason: str) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        match = await db(_get_match, community, match_id)
        staff = await actor(interaction)
        if match.status not in TERMINAL:

            async def void_live(confirmed: Interaction[Any]) -> None:
                await db(lobby.staff_void, match_id, staff, reason)
                await reply(confirmed, f"Match #{match_id} cancelled. No ratings changed.")

            await _send_confirm(
                interaction, f"Cancel live match #{match_id}? Nobody's rating changes.", void_live
            )
            return

        preview = await db(corrections.preview_outcome, match, "void")

        async def void_finished(confirmed: Interaction[Any]) -> None:
            await db(corrections.apply_outcome_correction, match_id, "void", staff, reason)
            await reply(confirmed, f"Match #{match_id} voided and ratings corrected.")

        text = f"Void finished match #{match_id}?\n{await db(_preview_text, preview)}"
        await _send_confirm(interaction, text, void_finished)

    await guarded(interaction, run)


def _team_of_player(match: Match, player: Player) -> str:
    row = match.participants.filter(player=player, removed_reason="").exclude(team="").first()
    if row is None:
        raise InvalidAction(f"{player.display_name} isn't on this match's roster.")
    return row.team


@match_group.command(name="rescore", description="Set or change a match's winner")
@app_commands.describe(winning_captain="The captain of the team that won")
async def match_rescore(
    interaction: Interaction[Any], match_id: int, winning_captain: discord.Member, reason: str
) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        match = await db(_get_match, community, match_id)
        staff = await actor(interaction)
        captain = await db(
            common.get_or_create_player, winning_captain.id, winning_captain.display_name
        )
        winner = await db(_team_of_player, match, captain)
        label = await db(boards.outcome_label, match, winner)
        if match.status == Status.DISPUTED:
            await db(results.resolve_dispute, match_id, winner, staff, reason)
            await reply(interaction, f"Match #{match_id}: {label} recorded as the winner.")
            return
        if match.status not in TERMINAL:
            raise InvalidAction("That match is still being played.")
        preview = await db(corrections.preview_outcome, match, winner)

        async def apply(confirmed: Interaction[Any]) -> None:
            await db(corrections.apply_outcome_correction, match_id, winner, staff, reason)
            await reply(confirmed, f"Match #{match_id} corrected: {label} won.")

        details = await db(_preview_text, preview)
        await _send_confirm(
            interaction, f"Make {label} the winner of #{match_id}?\n{details}", apply
        )

    await guarded(interaction, run)


@match_group.command(name="sub", description="Replace a player. The substitute takes the result")
async def match_sub(
    interaction: Interaction[Any],
    match_id: int,
    out: discord.Member,
    replacement: discord.Member,
    reason: str,
) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        match = await db(_get_match, community, match_id)
        staff = await actor(interaction)
        out_player = await db(common.get_or_create_player, out.id, out.display_name)
        in_player = await db(common.get_or_create_player, replacement.id, replacement.display_name)
        if match.status not in TERMINAL:
            await db(lobby.substitute_live, match_id, out_player, in_player, staff)
            await reply(interaction, f"{replacement.display_name} is in for {out.display_name}.")
            return
        preview = await db(corrections.preview_sub, match, out_player, in_player)

        async def apply(confirmed: Interaction[Any]) -> None:
            await db(
                corrections.apply_sub_correction, match_id, out_player, in_player, staff, reason
            )
            await reply(confirmed, "Roster corrected and ratings recalculated.")

        text = (
            f"Replace {out.display_name} with {replacement.display_name} on #{match_id}?\n"
            f"{await db(_preview_text, preview)}"
        )
        await _send_confirm(interaction, text, apply)

    await guarded(interaction, run)


def _explain_text(community: Community, match_id: int) -> str:
    match = _get_match(community, match_id)
    lines = [f"**Match #{match.pk}**: {match.status}, pinned to {match.mode_version}"]
    for result in match.results.order_by("revision"):
        label = boards.outcome_label(match, result.outcome)
        lines.append(f"Result rev {result.revision}: {label} ({result.source}) {result.reason}")
    for transition in match.transitions.order_by("id"):
        lines.append(
            f"<t:{int(transition.created_at.timestamp())}:T> {transition.from_status or 'new'} "
            f"-> {transition.to_status} ({transition.source})"
        )
    events = RatingEvent.objects.filter(match=match).select_related("player", "run").order_by("id")
    rows = [
        f"{event.player.display_name[:14]:<14} {event.delta:+4d} -> {event.rating_after} "
        f"[{event.run.kind}]"
        for event in events[:24]
    ]
    text = "\n".join(lines)
    if rows:
        text += "\n```\n" + "\n".join(rows) + "\n```"
    return text[:1900]


@match_group.command(name="explain", description="The audit trail for a match")
async def match_explain(interaction: Interaction[Any], match_id: int) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        await reply(interaction, await db(_explain_text, community, match_id))

    await guarded(interaction, run)


@rating_group.command(name="seed", description="Staff: set a player's rating for known skill")
async def rating_seed(interaction: Interaction[Any], player: discord.Member, rating: int) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        staff = await actor(interaction)
        target = await db(common.get_or_create_player, player.id, player.display_name)
        season = await db(common.open_season, community)
        await db(ratings.seed_player, season, target, rating, staff)
        await reply(interaction, f"{player.display_name} is now {rating}.")

    await guarded(interaction, run)


@rating_group.command(name="adjust", description="Staff: change a player's rating, with a reason")
async def rating_adjust(
    interaction: Interaction[Any], player: discord.Member, delta: int, reason: str
) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        staff = await actor(interaction)
        target = await db(common.get_or_create_player, player.id, player.display_name)
        season = await db(common.open_season, community)
        event = await db(ratings.adjust_player, season, target, delta, staff, reason)
        await reply(interaction, f"{player.display_name} is now {event.rating_after}.")

    await guarded(interaction, run)


@queue_group.command(name="kick", description="Staff: remove a player from the queue")
async def queue_kick(interaction: Interaction[Any], player: discord.Member) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        target = await db(common.get_or_create_player, player.id, player.display_name)
        removed = await db(queue.kick, community.pk, target)
        await reply(interaction, "Removed." if removed else "They weren't in the queue.")

    await guarded(interaction, run)


@queue_group.command(name="clear", description="Staff: empty the queue")
async def queue_clear(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        removed = await db(queue.clear, community.pk)
        await reply(interaction, f"Cleared {removed} players from the queue.")

    await guarded(interaction, run)


penalty_group = app_commands.Group(name="penalty", description="Staff: queue cooldowns")


@penalty_group.command(name="add", description="Put a player on a queue cooldown")
@app_commands.choices(
    kind=[
        app_commands.Choice(name="Dodge", value="dodge"),
        app_commands.Choice(name="Leaver", value="leaver"),
        app_commands.Choice(name="False report", value="false_report"),
        app_commands.Choice(name="Manual", value="manual"),
    ]
)
@app_commands.describe(minutes="Leave empty to use the escalating ladder for this kind")
async def penalty_add(
    interaction: Interaction[Any],
    player: discord.Member,
    kind: app_commands.Choice[str],
    reason: str,
    minutes: app_commands.Range[int, 1, 525600] | None = None,
) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        staff = await actor(interaction)
        target = await db(common.get_or_create_player, player.id, player.display_name)
        penalty = await db(
            penalties.issue,
            community,
            target,
            kind.value,
            reason=reason,
            issued_by=staff,
            minutes=minutes,
        )
        await reply(
            interaction,
            f"{player.display_name} can't queue until <t:{int(penalty.expires_at.timestamp())}:f>.",
        )

    await guarded(interaction, run)


@penalty_group.command(name="revoke", description="Lift a player's active cooldown")
async def penalty_revoke(interaction: Interaction[Any], player: discord.Member) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        target = await db(common.get_or_create_player, player.id, player.display_name)
        count = await db(penalties.revoke_active, community, target)
        await reply(interaction, f"Lifted {count} cooldown(s).")

    await guarded(interaction, run)


mode_group = app_commands.Group(name="mode", description="Staff: draft modes")


def _modes_text(community: Community) -> str:
    lines = []
    for version in modes.latest_versions():
        active = " (active)" if version.pk == community.active_mode_id else ""
        lines.append(f"`{version.slug}` v{version.version}: {version.name}{active}")
    return "\n".join(lines) or "No modes loaded."


@mode_group.command(name="list", description="Available draft modes")
async def mode_list(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        await reply(interaction, await db(_modes_text, community))

    await guarded(interaction, run)


def _activate_latest(community_id: int, slug: str) -> DraftModeVersion:
    version = DraftModeVersion.objects.filter(slug=slug).order_by("-version").first()
    if version is None:
        raise InvalidAction(f"There's no mode called `{slug}`.")
    try:
        return modes.activate(community_id, version.pk)
    except ModeValidationError as exc:
        raise InvalidAction("That mode can't run: " + "; ".join(exc.problems)) from exc


@mode_group.command(name="activate", description="Use this mode for new lobbies")
async def mode_activate(interaction: Interaction[Any], slug: str) -> None:
    async def run() -> None:
        community = await _staff_start(interaction)
        version = await db(_activate_latest, community.pk, slug)
        await db(common.render_queue, community)
        await reply(interaction, f"New lobbies now use {version}. Live matches keep their own.")

    await guarded(interaction, run)


roster_group = app_commands.Group(name="roster", description="Staff: champion list")


@roster_group.command(name="sync", description="Compare champions with the Deadlock API")
async def roster_sync(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await _staff_start(interaction)
        remote = await asyncio.to_thread(champions.fetch_remote)  # Never on the database thread.
        diff = await db(champions.diff, remote)
        if diff.is_empty:
            await reply(interaction, "The roster is up to date.")
            return
        lines = [f"New: {hero.name} (id {hero.id})" for hero in diff.new]
        lines += [f"Renamed: {old.name} -> {new}" for old, new in diff.renamed]
        lines += [f"Would be disabled: {champion.name}" for champion in diff.missing_upstream]

        async def apply(confirmed: Interaction[Any]) -> None:
            await db(champions.apply_diff, diff)
            await reply(confirmed, "Roster updated. New lobbies use it; live matches don't change.")

        await _send_confirm(interaction, "\n".join(lines)[:1900], apply)

    await guarded(interaction, run)


@app_commands.command(name="ledger_check", description="Staff: verify the rating ledger")
async def ledger_check(interaction: Interaction[Any]) -> None:
    async def run() -> None:
        await _staff_start(interaction)
        problems = await db(integrity.check_all)
        await reply(interaction, "\n".join(problems)[:1900] if problems else "Ledger OK.")

    await guarded(interaction, run)


GROUPS: tuple[app_commands.Group, ...] = (match_group, penalty_group, mode_group, roster_group)
COMMANDS: tuple[app_commands.Command[Any, Any, Any], ...] = (ledger_check,)
