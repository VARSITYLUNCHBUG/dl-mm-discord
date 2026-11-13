"""Bebot's look, shared by every draft message: names, emojis, colours and the turn timeline.

Ported from Bebot's bot/config/ui.py and bot/utils.py so drafts read the way the community knows.
Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import discord

HIDDEN_KING = "The Hidden King"
ARCHMOTHER = "The Archmother"
HK_EMOJI = "🔶"
AM_EMOJI = "🔷"
BAN_EMOJI = "🚫"
CAPTAIN_EMOJI = "👑"
COIN_EMOJI = "🪙"
CAPTAINS_SUMMARY = "Captains"
BANS_SUMMARY = "Bans"
DRAFT_TITLE = "🏆 Draft 🏆"
# discord.Color.orange() and discord.Color.blue(), which Bebot used for each side's turn.
HK_COLOUR = 0xE67E22
AM_COLOUR = 0x3498DB
COINFLIP_COLOUR = 0x800080
# What Bebot appended to a hero chosen for the captain.
RANDOM_CHOICE = " (random choice)"
RANDOMED = " (randomed)"
TIMELINE_RULE = "─────────────────────────────────────"

SIDES = ("hk", "am")


def side_name(side: str) -> str:
    return HIDDEN_KING if side == "hk" else ARCHMOTHER


def side_emoji(side: str) -> str:
    return HK_EMOJI if side == "hk" else AM_EMOJI


def side_colour(side: str) -> int:
    return HK_COLOUR if side == "hk" else AM_COLOUR


def other_side(side: str) -> str:
    return "am" if side == "hk" else "hk"


def draft_title(number: int | None, name: str = "") -> str:
    if number is not None and name:
        return f"🏆 Draft #{number}: {name} 🏆"
    if number is not None:
        return f"🏆 Draft #{number} 🏆"
    return DRAFT_TITLE


def countdown(deadline_at: datetime | None) -> str:
    """Bebot's " ⌛ 40s remaining", as a timestamp Discord counts down without edits."""
    if deadline_at is None:
        return ""
    return f" ⌛ <t:{int(deadline_at.timestamp())}:R>"


def hero_emoji_text(name: str, emoji_id: int | None, unicode_emoji: str) -> str:
    """The emoji to show before a hero's name in text, or empty if it has none."""
    if emoji_id:
        alias = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_") or "hero"
        return f"<:{alias[:32]}:{emoji_id}>"
    return unicode_emoji


def hero_partial_emoji(emoji_id: int | None, unicode_emoji: str) -> discord.PartialEmoji | None:
    """The emoji for a hero's button. Custom emojis need only their ID."""
    if emoji_id:
        return discord.PartialEmoji(name="c", id=emoji_id)
    if unicode_emoji:
        return discord.PartialEmoji(name=unicode_emoji)
    return None


def hero_line(emoji: str, name: str, *, random_choice: bool = False, auto: bool = False) -> str:
    suffix = RANDOM_CHOICE if random_choice else RANDOMED if auto else ""
    return f"{emoji} {name}{suffix}".strip()


def hero_list(lines: Sequence[str]) -> str:
    """Bebot printed "None" for an empty list, and so do we."""
    return "\n".join(lines) if lines else "None"


def timeline(steps: Sequence[tuple[str, str]], current: int | None) -> str:
    """Bebot's turn strip: bans on one line, picks on the next, the current turn in brackets.

    `steps` is (action, side) for each ban or pick, in order. A faithful port of Bebot's
    generate_turn_str, spacing quirks included.
    """
    ban_parts: list[str] = []
    pick_parts: list[str] = []
    last_action: str | None = None
    first_pick_seen = False
    first_ban_after_pick_seen = False
    in_second_ban = False
    for index, (action, side) in enumerate(steps):
        emoji = side_emoji(side)
        if index == current:
            emoji = f"[{emoji}]"
        if last_action is not None and last_action != action:
            if not first_pick_seen and action != "ban":
                ban_parts.append("  ")
                pick_parts.append("  ")
                first_pick_seen = True
            elif first_pick_seen and not first_ban_after_pick_seen and action == "ban":
                ban_parts.append("    ")
                pick_parts.append("")
                first_ban_after_pick_seen = True
                in_second_ban = True
        if action == "ban":
            ban_parts.append(emoji)
            pick_parts.append(" " * len(emoji))
        else:
            if in_second_ban:
                pick_parts.append(" " * len(emoji))
                in_second_ban = False
            pick_parts.append(emoji)
            ban_parts.append(" " * len(emoji))
        last_action = action
    ban_str = "".join(ban_parts).rstrip()
    pick_str = "".join(pick_parts).rstrip()
    if any(char != " " for char in ban_str):
        return f"Bans\n{ban_str}\n{TIMELINE_RULE}\nPicks\n{pick_str}"
    emojis = [side_emoji(side) for _action, side in steps]
    if current is not None and 0 <= current < len(emojis):
        emojis[current] = f"[{emojis[current]}]"
    return " ".join(emojis)


def timeline_block(steps: Sequence[tuple[str, str]], current: int | None) -> str:
    return f"```\n{timeline(steps, current)}\n```"
