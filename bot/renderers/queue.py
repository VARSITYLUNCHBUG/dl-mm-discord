from __future__ import annotations

from typing import Any

from discord import ButtonStyle
from discord.ui import ActionRow, Container, LayoutView, TextDisplay

from bot.routing import RouteButton
from league.services.boards import QueueView

ACCENT = 0x5865F2
MAX_NAMES_SHOWN = 30


def render_queue(view_model: QueueView) -> LayoutView:
    names = view_model.players[:MAX_NAMES_SHOWN]
    listing = "\n".join(f"{index}. {name}" for index, name in enumerate(names, start=1)) or "Empty"
    if len(view_model.players) > MAX_NAMES_SHOWN:
        listing += f"\n...and {len(view_model.players) - MAX_NAMES_SHOWN} more"
    needed = view_model.capacity - view_model.size
    heading = f"## Queue: {view_model.size}/{view_model.capacity}"
    if 0 < needed < view_model.capacity:
        heading += f" · {needed} more to start"
    live = view_model.live_matches
    room = (
        f"<#{view_model.waiting_room_channel_id}>"
        if view_model.waiting_room_channel_id
        else "the Waiting Room voice channel"
    )
    box: Container[Any] = Container(
        TextDisplay(f"{heading}\n{listing}"),
        ActionRow(
            RouteButton("join", label="Join", style=ButtonStyle.success),
            RouteButton("leave", label="Leave", style=ButtonStyle.danger),
        ),
        TextDisplay(
            f"-# {view_model.mode_name} · {live} match{'' if live == 1 else 'es'} in progress · "
            f"Wait in {room} and you're moved in automatically."
        ),
        accent_colour=ACCENT,
    )
    view = LayoutView(timeout=None)
    view.add_item(box)
    return view
