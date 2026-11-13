from __future__ import annotations

from typing import Any

import discord
from discord import ButtonStyle
from discord.ui import ActionRow, Container, LayoutView, MediaGallery, Separator, TextDisplay

from bot.renderers.board import player_mention
from bot.routing import RouteButton
from league.services.boards import CorrectionView, LeaderboardRow, ResultRow, ResultsView

ACCENT = 0x57F287
ACCENT_CORRECTION = 0xFEE75C
FIGURE_SPACE = "\u2007"  # As wide as a digit, and Discord doesn't collapse it.
AWARD_MARKS = {"mvp": "🏅", "key": "⭐"}


def _quote_lines(rows: list[tuple[int, str, int, int]]) -> str:
    """Quoted lines of "> **+12** | __1512__ | @Name", from (user ID, name, delta, new rating).

    The name goes last because Discord has no tabs or columns outside code blocks, and names vary
    in width. Number-width spaces pad the numbers, so the pipes line up.
    """
    deltas = [f"{delta:+d}" for _uid, _name, delta, _rating in rows]
    ratings = [str(rating) for _uid, _name, _delta, rating in rows]
    delta_width = max(map(len, deltas), default=0)
    rating_width = max(map(len, ratings), default=0)
    lines = []
    for (uid, name, _delta, _rating), delta, rating in zip(rows, deltas, ratings, strict=True):
        delta_pad = FIGURE_SPACE * (delta_width - len(delta))
        rating_pad = FIGURE_SPACE * (rating_width - len(rating))
        lines.append(
            f"> {delta_pad}**{delta}** | {rating_pad}__{rating}__ | {player_mention(uid, name)}"
        )
    return "\n".join(lines)


def team_table(rows: list[ResultRow]) -> str:
    """The rating lines, with 🏅 MVP and ⭐ key-player marks and the scoreboard stats under each
    player when there was a read."""
    marked = [
        (r.discord_user_id, f"{r.name}", r.delta, r.after) for r in rows
    ]  # The mark goes after the mention, so the number columns still line up.
    lines = _quote_lines(marked).split("\n") if rows else []
    out = []
    for row, line in zip(rows, lines, strict=True):
        mark = AWARD_MARKS.get(row.award, "")
        out.append(f"{line} {mark}".rstrip())
        if row.stat_line:
            out.append(f"> -# {row.stat_line}")
    return "\n".join(out)


def _team_section(view_model: ResultsView, team: str) -> str:
    on_a = team == "A"
    rows = view_model.team_a if on_a else view_model.team_b
    champions = view_model.champions_a if on_a else view_model.champions_b
    average = view_model.avg_a if on_a else view_model.avg_b
    trophy = "🏆 " if view_model.winner == team else ""
    header = f"{trophy}**{view_model.team_name(team)}** · avg {average:.0f}"
    section = f"{header}\n{team_table(rows)}"
    if champions:
        section += f"\n{', '.join(champions)}"
    return section


def render_results(view_model: ResultsView, *, attachment_name: str | None) -> LayoutView:
    extras = []
    if view_model.duration_s:
        extras.append(f"{view_model.duration_s // 60}:{view_model.duration_s % 60:02d}")
    if view_model.deadlock_match_id:
        extras.append(f"Deadlock match {view_model.deadlock_match_id}")
    tail = "".join(f" · {extra}" for extra in extras)
    title = (
        f"## Match #{view_model.match_id}: {view_model.team_name(view_model.winner)} won\n"
        f"-# {view_model.team_name('A')} had a {view_model.expected_a:.0%} expected chance{tail}"
    )
    box: Container[Any] = Container(TextDisplay(title), accent_colour=ACCENT)
    box.add_item(TextDisplay(_team_section(view_model, "A")))
    box.add_item(TextDisplay(_team_section(view_model, "B")))
    if view_model.bans:
        box.add_item(Separator())
        box.add_item(TextDisplay(f"**Bans:** {', '.join(view_model.bans)}"))
    if attachment_name:
        box.add_item(MediaGallery(discord.MediaGalleryItem(f"attachment://{attachment_name}")))
    view = LayoutView(timeout=None)
    view.add_item(box)
    return view


def render_correction(view_model: CorrectionView) -> LayoutView:
    if view_model.lines:
        changes = _quote_lines(
            [(ln.discord_user_id, ln.name, ln.net, ln.rating) for ln in view_model.lines]
        )
    else:
        changes = "No rating changes."
    extra = f"\n-# ...and {view_model.more} more" if view_model.more else ""
    box: Container[Any] = Container(
        TextDisplay(
            f"## Match #{view_model.match_id} corrected: {view_model.summary}\n"
            f"Reason: {view_model.reason or 'none given'}\n{changes}{extra}"
        ),
        accent_colour=ACCENT_CORRECTION,
    )
    view = LayoutView(timeout=None)
    view.add_item(box)
    return view


def leaderboard_text(rows: list[LeaderboardRow], minimum_games: int) -> str:
    if not rows:
        return f"No one has played {minimum_games} games yet."
    body = "\n".join(
        f"{row.rank:>2}. {row.name[:16]:<16} {row.rating:>4}  {row.wins}-{row.losses}"
        for row in rows
    )
    return f"```\n{body}\n```"


def render_scoreboard_notice(match_id: int, read_id: int, unmapped: int) -> LayoutView:
    """Ask the captains to match the scoreboard names the reader couldn't place."""
    noun = "name needs" if unmapped == 1 else "names need"
    box: Container[Any] = Container(
        TextDisplay(
            f"**Scoreboard read:** {unmapped} {noun} matching to players. A captain can match "
            "them, and the bot remembers in-game names for next time."
        ),
        ActionRow(
            RouteButton(
                "ocr_map", match_id, read_id, label="Match names", style=ButtonStyle.primary
            )
        ),
    )
    view = LayoutView(timeout=None)
    view.add_item(box)
    return view
