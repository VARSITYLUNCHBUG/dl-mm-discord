"""Bebot's priority list message. Pure: no I/O."""

from __future__ import annotations

from typing import Any

from discord.ui import Container, LayoutView, TextDisplay

from bot.renderers.bebot import AM_COLOUR
from league.services.prio import PrioView

# Discord's Color.blue(), as Bebot's list used.
PRIO_COLOUR = AM_COLOUR
LEGEND = ("🟢 In queue", "⏳ Grace period")
MAX_LINES = 60


def prio_text(view_model: PrioView) -> str:
    lines = []
    for index, line in enumerate(view_model.lines[:MAX_LINES], start=1):
        if line.grace_minutes is not None:
            wait = f"({line.minutes}m, Grace: {line.grace_minutes}m)"
        else:
            wait = f"({line.minutes}m)"
        lines.append(f"`{index}.` {line.status} **{line.name}** {wait}")
    if len(view_model.lines) > MAX_LINES:
        lines.append(f"...and {len(view_model.lines) - MAX_LINES} more")
    return "\n".join(lines) if lines else "Queue is empty!"


def render_prio(view_model: PrioView) -> LayoutView:
    legend = "\n".join(f"-# {item}" for item in LEGEND)
    box: Container[Any] = Container(
        TextDisplay(f"### Priority Queue\n{prio_text(view_model)}"),
        TextDisplay(legend),
        accent_colour=PRIO_COLOUR,
    )
    view = LayoutView(timeout=None)
    view.add_item(box)
    return view
