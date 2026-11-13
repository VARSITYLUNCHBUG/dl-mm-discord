"""Bebot's standalone draft message and its hero grid, the captain coinflip and /randomdraft.
Pure: no I/O."""

from __future__ import annotations

from typing import Any

import discord
from discord import ButtonStyle
from discord.ui import ActionRow, Container, LayoutView, MediaGallery, TextDisplay

from bot.renderers import bebot
from bot.renderers.board import BUTTONS_PER_ROW, SELECT_LIMIT, grid_fits, grid_messages
from bot.routing import RouteButton, RouteSelect
from league.models.quickdraft import QuickDraftStatus
from league.services.quickdraft import (
    ARCHMOTHER,
    CHOICE_LABELS,
    FIRST_PICK,
    HIDDEN_KING,
    LAST_PICK,
    HeroChoice,
    HeroOption,
    QuickDraftState,
    RandomDraft,
)


def _view(*items: discord.ui.Item[Any]) -> LayoutView:
    view = LayoutView(timeout=None)
    for item in items:
        view.add_item(item)
    return view


# --- Captains -------------------------------------------------------------------------------


def _captain_buttons(state: QuickDraftState) -> ActionRow[Any]:
    """Bebot's two Become Captain buttons. A claimed side's button is disabled."""
    return ActionRow(
        RouteButton(
            "qd_captain",
            state.id,
            "hk",
            label=f"{bebot.HK_EMOJI} Become {bebot.HIDDEN_KING} Captain",
            style=ButtonStyle.danger,
            disabled=state.hk_captain_id is not None,
        ),
        RouteButton(
            "qd_captain",
            state.id,
            "am",
            label=f"{bebot.AM_EMOJI} Become {bebot.ARCHMOTHER} Captain",
            style=ButtonStyle.primary,
            disabled=state.am_captain_id is not None,
        ),
    )


# --- Coinflip -------------------------------------------------------------------------------


_CHOICE_STYLE = {
    FIRST_PICK: ButtonStyle.primary,
    LAST_PICK: ButtonStyle.primary,
    HIDDEN_KING: ButtonStyle.danger,
    ARCHMOTHER: ButtonStyle.primary,
}
_CHOICE_EMOJI = {HIDDEN_KING: bebot.HK_EMOJI, ARCHMOTHER: bebot.AM_EMOJI}


def _choice_label(choice: str) -> str:
    emoji = _CHOICE_EMOJI.get(choice)
    return f"{emoji} {CHOICE_LABELS[choice]}" if emoji else CHOICE_LABELS[choice]


def _coinflip(state: QuickDraftState) -> LayoutView:
    title = f"### {bebot.COIN_EMOJI} Captain Coinflip"
    if not state.winner_choice:
        body = (
            f"**{state.coinflip_winner_name}** wins the flip!\n\n"
            f"<@{state.coinflip_winner_id}>, choose your preference:"
        )
    else:
        body = f"**{state.coinflip_loser_name}**, choose your preference:"
    buttons = [
        RouteButton(
            "qd_coin",
            state.id,
            choice,
            label=_choice_label(choice),
            style=_CHOICE_STYLE[choice],
        )
        for choice in state.options
    ]
    box: Container[Any] = Container(
        TextDisplay(f"{title}\n{body}"), accent_colour=bebot.COINFLIP_COLOUR
    )
    return _view(box, ActionRow(*buttons))


def _coinflip_done(state: QuickDraftState) -> LayoutView:
    def chose(choice: str) -> str:
        return CHOICE_LABELS.get(choice, choice.replace("_", " ").title())

    box: Container[Any] = Container(
        TextDisplay(
            f"### {bebot.COIN_EMOJI} Captain Coinflip - Complete\n"
            f"**{state.coinflip_winner_name}** chose: {chose(state.winner_choice)}\n"
            f"**{state.coinflip_loser_name}** chose: {chose(state.loser_choice)}\n\n"
            "Ready to start the draft!"
        ),
        accent_colour=bebot.COINFLIP_COLOUR,
    )
    start = RouteButton("qd_start", state.id, label="Start Draft", style=ButtonStyle.success)
    return _view(box, ActionRow(start))


# --- Hero draft -----------------------------------------------------------------------------


def _hero_text(choice: HeroChoice) -> str:
    emoji = bebot.hero_emoji_text(choice.name, choice.emoji_id, choice.unicode_emoji)
    return bebot.hero_line(emoji, choice.name, random_choice=choice.random_choice, auto=choice.auto)


def _captains(state: QuickDraftState, final: bool) -> str:
    rows = [f"{bebot.CAPTAIN_EMOJI} **{bebot.CAPTAINS_SUMMARY}**"]
    for side in bebot.SIDES:
        user_id, name = state.captain(side)
        emoji = bebot.side_emoji(side)
        if not final and side == state.acting_side:
            timer = bebot.countdown(state.deadline_at)
            rows.append(f"{emoji} <@{user_id}> {state.step_action} a hero!{timer}")
        else:
            rows.append(f"{emoji} {name}")
    return "\n".join(rows)


def summary_text(state: QuickDraftState, *, final: bool) -> str:
    """Bebot's create_draft_summary_embed, line for line."""
    bans = [_hero_text(c) for c in state.actions if c.action == "ban"]
    parts = [
        _captains(state, final),
        "",
        f"{bebot.BAN_EMOJI} **{bebot.BANS_SUMMARY}**",
        bebot.hero_list(bans),
    ]
    for side in bebot.SIDES:
        picks = [_hero_text(c) for c in state.actions if c.action == "pick" and c.side == side]
        parts += ["", f"{bebot.side_emoji(side)} **{bebot.side_name(side)}**"]
        parts.append(bebot.hero_list(picks))
    if not final:
        parts += ["", "", bebot.timeline_block(state.steps, state.step_index)]
    else:
        parts += ["", "# GLHF"]
    return "\n".join(parts)


def _title(state: QuickDraftState) -> str:
    return f"## {bebot.draft_title(state.id, state.draft_name)}"


def _hero_button(state: QuickDraftState, hero: HeroOption) -> RouteButton:
    return RouteButton(
        "qd_hero",
        state.id,
        f"{state.step_index}.{hero.id}",
        label=hero.name[:80],
        style=ButtonStyle.secondary,
        disabled=hero.taken,
        emoji=bebot.hero_partial_emoji(hero.emoji_id, hero.unicode_emoji),
    )


def hero_grid(state: QuickDraftState) -> list[LayoutView]:
    """The hero grid, in messages of their own below the draft message: the match flow's grid.

    Every hero keeps its place, taken ones greyed out, with 🎲 Random last. A roster too big for
    two messages falls back to dropdowns, as the match board does.
    """
    if state.status != QuickDraftStatus.DRAFTING:
        return []
    random: ActionRow[Any] = ActionRow(
        RouteButton(
            "qd_random",
            state.id,
            state.step_index,
            label="Random",
            style=ButtonStyle.primary,
            emoji=discord.PartialEmoji(name="🎲"),
        )
    )
    buttons = [_hero_button(state, hero) for hero in state.heroes]
    rows: list[ActionRow[Any]] = [
        ActionRow(*buttons[start : start + BUTTONS_PER_ROW])
        for start in range(0, len(buttons), BUTTONS_PER_ROW)
    ]
    rows.append(random)
    if not grid_fits(rows):
        rows = [*_hero_selects(state), random]
    return grid_messages(rows)


def _hero_selects(state: QuickDraftState) -> list[ActionRow[Any]]:
    noun = "Ban" if state.step_action == "ban" else "Pick"
    options = [
        discord.SelectOption(
            label=hero.name[:100],
            value=str(hero.id),
            emoji=bebot.hero_partial_emoji(hero.emoji_id, hero.unicode_emoji),
        )
        for hero in state.available
    ]
    chunks = [
        options[start : start + SELECT_LIMIT] for start in range(0, len(options), SELECT_LIMIT)
    ]
    return [
        ActionRow(
            RouteSelect(
                "qd_pick",
                state.id,
                f"{state.step_index}.{index}",
                options=chunk,
                placeholder=f"{noun} a hero: {chunk[0].label} to {chunk[-1].label}"[:150],
            )
        )
        for index, chunk in enumerate(chunks)
    ]


def _drafting(state: QuickDraftState) -> LayoutView:
    box: Container[Any] = Container(
        TextDisplay(f"{_title(state)}\n{summary_text(state, final=False)}"),
        accent_colour=bebot.side_colour(state.acting_side or state.first_side),
    )
    return _view(box)


def _completed(state: QuickDraftState) -> LayoutView:
    last_side = state.steps[-1][1] if state.steps else state.first_side
    box: Container[Any] = Container(
        TextDisplay(f"{_title(state)}\n{summary_text(state, final=True)}"),
        accent_colour=bebot.side_colour(last_side),
    )
    if state.gif_url:
        box.add_item(MediaGallery(discord.MediaGalleryItem(state.gif_url)))
    return _view(box)


def render_quick_draft(state: QuickDraftState) -> LayoutView:
    if state.status == QuickDraftStatus.CAPTAINS:
        # Bebot's /startdraft posted the two buttons and nothing else.
        return _view(_captain_buttons(state))
    if state.status == QuickDraftStatus.COINFLIP:
        return _coinflip(state)
    if state.status == QuickDraftStatus.READY:
        return _coinflip_done(state)
    if state.status == QuickDraftStatus.DRAFTING:
        return _drafting(state)
    if state.status == QuickDraftStatus.COMPLETED:
        return _completed(state)
    return _view(TextDisplay("Draft stopped."))


# --- /coinflip and /randomdraft -------------------------------------------------------------


def render_coinflip(winner: tuple[int, str], loser: tuple[int, str]) -> LayoutView:
    """Bebot's /coinflip: who should take The Hidden King (first pick)."""
    box: Container[Any] = Container(
        TextDisplay(
            f"### {bebot.COIN_EMOJI} Captain Coinflip\n"
            f"**{winner[1]}** wins the flip!\n\n"
            f"<@{winner[0]}> should select {bebot.HIDDEN_KING} (first pick)\n"
            f"<@{loser[0]}> should select {bebot.ARCHMOTHER}"
        ),
        accent_colour=bebot.COINFLIP_COLOUR,
    )
    return _view(box)


def _option_list(heroes: list[HeroOption]) -> str:
    return (
        "\n".join(
            bebot.hero_line(bebot.hero_emoji_text(h.name, h.emoji_id, h.unicode_emoji), h.name)
            for h in heroes
        )
        or "None"
    )


def render_random_draft(draft: RandomDraft) -> LayoutView:
    parts = [f"## {bebot.DRAFT_TITLE} (Random)"]
    for index, side in enumerate(bebot.SIDES):
        if index:
            parts += ["", ""]
        parts += [
            f"{bebot.side_emoji(side)} **{bebot.side_name(side)}**",
            "",
            "**Bans:**",
            _option_list(draft.bans[side]),
            "",
            "**Picks:**",
            _option_list(draft.picks[side]),
        ]
    box: Container[Any] = Container(TextDisplay("\n".join(parts)), accent_colour=bebot.HK_COLOUR)
    return _view(box)
