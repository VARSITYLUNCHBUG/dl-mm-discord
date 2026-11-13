"""State to components. Pure: given a BoardState, returns a Components V2 view with no I/O."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import discord
from discord import ButtonStyle, SelectOption
from discord.ui import ActionRow, Container, LayoutView, MediaGallery, Separator, TextDisplay

from bot.renderers import bebot
from bot.routing import RouteButton, RouteSelect
from league.domain.engine import Status
from league.models.subs import SubStatus
from league.services import dev
from league.services.boards import BoardState, ChampionOption, DraftLine, PlayerLine, SubLine
from league.services.subs import OfferInfo

# Discord caps a string select at 25 options, and an action row at 5 buttons.
SELECT_LIMIT = 25
BUTTONS_PER_ROW = 5
# A message holds 40 components, counting each row and button, so a message of nothing but rows
# holds 6 full rows. The champion grid gets up to two such messages of its own below the board; a
# bigger roster falls back to selects.
ROWS_PER_GRID_MESSAGE = 6
GRID_MESSAGES = 2
ACCENT_LIVE = 0x5865F2
ACCENT_ATTENTION = 0xFEE75C
ACCENT_DONE = 0x57F287
ACCENT_VOID = 0x80848E

STATUS_TITLES = {
    Status.READY_CHECK: "Ready check",
    Status.CAPTAIN_PHASE: "Captain vote",
    Status.PLAYER_DRAFT: "Player draft",
    Status.SIDE_SELECT: "Choose sides",
    Status.CHAMPION_DRAFT: "Champion draft",
    Status.LOBBY_CODE: "Lobby code",
    Status.IN_PROGRESS: "Game on",
    Status.AWAITING_CONFIRMATION: "Confirm the result",
    Status.DISPUTED: "Disputed: waiting for staff",
    Status.COMPLETED: "Finished",
    Status.VOIDED: "Cancelled",
}

CHECKLIST = "Lobby settings: Normal mode, bots off, cheats off, duplicate heroes off."


def player_mention(discord_user_id: int, name: str) -> str:
    """A real mention. Fake test players have no account, so they show their name."""
    return f"@{name}" if dev.is_fake(discord_user_id) else f"<@{discord_user_id}>"


# What happens when each timer runs out. Each reads on from Discord's "in 18 seconds".
TIMER_LABELS = {
    Status.READY_CHECK: "Ready check closes",
    Status.CAPTAIN_PHASE: "Vote closes",
    Status.PLAYER_DRAFT: "Auto-pick",
    Status.SIDE_SELECT: "Coin flip",
    Status.CHAMPION_DRAFT: "Auto-pick",
    Status.LOBBY_CODE: "Code due",
    Status.AWAITING_CONFIRMATION: "Confirms automatically",
}


def _stamp(moment: datetime, style: str = "R") -> str:
    return f"<t:{int(moment.timestamp())}:{style}>"


def countdown(state: BoardState) -> str:
    if state.on_hold:
        until = state.hold_until
        cancel = (
            f" The match is cancelled {_stamp(until)} if nobody takes the spot." if until else ""
        )
        return f"⏸ **Paused for a sub.**{cancel}"
    # A dispute's timer only re-pings staff, so players don't need to see it. The game-on timer
    # is a footer: it's a fallback, not the first thing players should read as the game starts.
    if state.deadline_at is None or state.status in {Status.DISPUTED, Status.IN_PROGRESS}:
        return ""
    label = TIMER_LABELS.get(state.status, "Ends")
    return f"{label} <t:{int(state.deadline_at.timestamp())}:R>"


def _team(state: BoardState, team: str) -> list[PlayerLine]:
    lines = [player for player in state.players if player.team == team]
    return sorted(lines, key=lambda line: not line.is_captain)


def _team_heading(state: BoardState, team: str, lines: list[PlayerLine]) -> str:
    average = sum(line.rating for line in lines) // len(lines)
    side = ""
    if state.hidden_king_team:
        side = f" · {bebot.side_name(state.side_of(team))}"
    return f"**{state.team_name(team)}**{side} · avg {average}"


def _team_block(state: BoardState, team: str) -> str:
    lines = _team(state, team)
    if not lines:
        return f"**{state.team_name(team)}**\n-"
    rows = [f"{'👑 ' if line.is_captain else ''}{line.name} ({line.rating})" for line in lines]
    return f"{_team_heading(state, team, lines)}\n" + "\n".join(rows)


# Phases whose timer sits on the acting captain's line, as Bebot showed it.
_TIMER_ON_CAPTAIN = frozenset({Status.PLAYER_DRAFT, Status.CHAMPION_DRAFT})
_DRAFT_TITLED = frozenset({Status.CHAMPION_DRAFT, Status.LOBBY_CODE})


def _header(state: BoardState) -> str:
    if state.status in _DRAFT_TITLED:
        line = f"## {bebot.draft_title(state.match_id, state.draft_name)}"
        paused = countdown(state) if state.on_hold else ""
        return f"{line}\n{paused}" if paused else line
    title = STATUS_TITLES.get(state.status, state.status)
    timer = "" if state.status in _TIMER_ON_CAPTAIN and not state.on_hold else countdown(state)
    line = f"## Match #{state.match_id}: {title}"
    return f"{line}\n{timer}" if timer else line


def _mention(line: PlayerLine) -> str:
    return player_mention(line.discord_user_id, line.name)


def _captains_block(state: BoardState, teams: tuple[str, str], verb: str, noun: str) -> str:
    """Bebot's 👑 Captains block: the captain on the clock is mentioned with what to do."""
    rows = [f"{bebot.CAPTAIN_EMOJI} **{bebot.CAPTAINS_SUMMARY}**"]
    for team in teams:
        captain = state.captain_line(team)
        if captain is None:
            continue
        prefix = f"{bebot.side_emoji(state.side_of(team))} " if state.hidden_king_team else ""
        if team == state.acting_team and verb and not state.on_hold:
            timer = bebot.countdown(state.deadline_at)
            rows.append(f"{prefix}{_mention(captain)} {verb} a {noun}!{timer}")
        else:
            rows.append(f"{prefix}{captain.name}")
    return "\n".join(rows)


def _side_order(state: BoardState) -> tuple[str, str]:
    """Hidden King first, as Bebot listed the sides."""
    hidden_king = state.hidden_king_team or "A"
    return (hidden_king, "B" if hidden_king == "A" else "A")


def _hero_text(line: DraftLine) -> str:
    emoji = bebot.hero_emoji_text(line.label, line.emoji_id, line.unicode_emoji)
    return bebot.hero_line(emoji, line.label, random_choice=line.random_choice, auto=line.auto)


def _heroes(state: BoardState, action: str, team: str | None = None) -> str:
    return bebot.hero_list(
        [
            _hero_text(line)
            for line in state.actions
            if line.action == action and (team is None or line.team == team)
        ]
    )


def _roster(state: BoardState, team: str) -> str:
    names = " · ".join(
        f"{'👑 ' if line.is_captain else ''}{line.name}" for line in _team(state, team)
    )
    return f"-# {names}" if names else ""


def _draft_summary(state: BoardState) -> str:
    """Bebot's draft message: captains, bans, then each side's heroes."""
    teams = _side_order(state)
    current = state.status == Status.CHAMPION_DRAFT
    verb = state.step_action if current else ""
    parts = [
        _captains_block(state, teams, verb, "hero"),
        "",
        f"{bebot.BAN_EMOJI} **{bebot.BANS_SUMMARY}**",
        _heroes(state, "ban"),
    ]
    for team in teams:
        side = state.side_of(team)
        parts += ["", f"{bebot.side_emoji(side)} **{bebot.side_name(side)}**"]
        roster = _roster(state, team)
        if roster:
            parts.append(roster)
        parts.append(_heroes(state, "pick", team))
    if current and state.champion_steps:
        steps = [(action, state.side_of(team)) for action, team in state.champion_steps]
        parts += ["", bebot.timeline_block(steps, state.champion_step_index)]
    return "\n".join(parts)


def _chunks(items: list[Any], size: int = SELECT_LIMIT) -> list[list[Any]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def _ready(state: BoardState, box: Container[Any]) -> None:
    ready = sum(player.is_ready for player in state.players)
    rows = [f"{'✅' if p.is_ready else '⏳'} {p.name} ({p.rating})" for p in state.players]
    box.add_item(TextDisplay(f"{ready}/{len(state.players)} ready\n" + "\n".join(rows)))
    box.add_item(
        ActionRow(RouteButton("ready", state.match_id, label="Ready", style=ButtonStyle.success))
    )


def _captain_vote(state: BoardState, box: Container[Any]) -> None:
    voted = len(state.voted_ids)
    rows = [
        f"{'✅' if p.player_id in state.voted_ids else '⏳'} {p.name} ({p.rating})"
        for p in state.players
    ]
    box.add_item(
        TextDisplay(
            f"Vote for up to two captains. {voted}/{len(state.players)} voted. "
            "Most votes wins; Elo breaks ties and fills any gap.\n" + "\n".join(rows)
        )
    )
    options = [
        SelectOption(label=f"{p.name} ({p.rating})"[:100], value=str(p.player_id))
        for p in state.players[:SELECT_LIMIT]
    ]
    box.add_item(
        ActionRow(
            RouteSelect(
                "captain_vote",
                state.match_id,
                options=options,
                placeholder="Vote for up to two captains",
                min_values=1,
                max_values=min(2, len(options)),
            )
        )
    )


def _player_draft(state: BoardState, box: Container[Any]) -> None:
    acting = state.acting_team or "A"
    teams = (acting, "B" if acting == "A" else "A")
    box.add_item(TextDisplay(_captains_block(state, teams, "pick", "player")))
    progress = f"Pick {state.turn_position} of {state.turn_total}." if state.turn_total > 1 else ""
    hint = f"{progress}{_next_up(state)}".strip()
    if hint:
        box.add_item(TextDisplay(f"-# {hint}"))
    box.add_item(Separator())
    box.add_item(TextDisplay(_team_block(state, "A") + "\n\n" + _team_block(state, "B")))
    # Drafted players stay in place, greyed out: a double click can't land on the next name.
    pool = [player for player in state.players if not player.is_captain]
    if not any(not player.team for player in pool):
        return
    for chunk in _chunks(pool, BUTTONS_PER_ROW):
        box.add_item(ActionRow(*[_player_button(state, player) for player in chunk]))


def _side_select(state: BoardState, box: Container[Any]) -> None:
    box.add_item(TextDisplay(_team_block(state, "A") + "\n\n" + _team_block(state, "B")))
    box.add_item(Separator())
    box.add_item(
        TextDisplay(
            f"Captains: claim a side. **{bebot.HIDDEN_KING} bans and picks first.** "
            "The first claim decides both sides; if nobody claims, it's a coin flip."
        )
    )
    box.add_item(
        ActionRow(
            RouteButton(
                "claim_side",
                state.match_id,
                "hk",
                label=f"Become {bebot.HIDDEN_KING} Captain",
                style=ButtonStyle.danger,
                emoji=discord.PartialEmoji(name=bebot.HK_EMOJI),
            ),
            RouteButton(
                "claim_side",
                state.match_id,
                "am",
                label=f"Become {bebot.ARCHMOTHER} Captain",
                style=ButtonStyle.primary,
                emoji=discord.PartialEmoji(name=bebot.AM_EMOJI),
            ),
        )
    )


def _next_up(state: BoardState) -> str:
    if not state.next_captain_name:
        return ""
    verb = {"ban": " bans", "pick": " picks"}.get(state.next_action, "")
    return f" Next: **{state.next_captain_name}**{verb}."


def _player_button(state: BoardState, player: PlayerLine) -> RouteButton:
    return RouteButton(
        "pick_player",
        state.match_id,
        f"{state.step_index}.{player.player_id}",
        label=f"{player.name} ({player.rating})"[:80],
        style=ButtonStyle.secondary if player.team else ButtonStyle.primary,
        disabled=bool(player.team),
    )


def _champion_emoji(champion: ChampionOption) -> discord.PartialEmoji | None:
    return bebot.hero_partial_emoji(champion.emoji_id, champion.unicode_emoji)


def _champion_button(state: BoardState, champion: ChampionOption) -> RouteButton:
    # Bebot's grey hero buttons. Taken champions stay in place, greyed out, so nothing shifts.
    return RouteButton(
        "choose_champion",
        state.match_id,
        f"{state.step_index}.{champion.id}",
        label=champion.name[:80],
        style=ButtonStyle.secondary,
        disabled=champion.taken,
        emoji=_champion_emoji(champion),
    )


def _champion_rows(state: BoardState, champions: list[ChampionOption]) -> list[ActionRow[Any]]:
    return [
        ActionRow(*[_champion_button(state, champion) for champion in chunk])
        for chunk in _chunks(champions, BUTTONS_PER_ROW)
    ]


def _champion_selects(state: BoardState) -> list[ActionRow[Any]]:
    """Only for a roster too big for the grid: dropdowns take far fewer components."""
    placeholder = "Ban" if state.step_action == "ban" else "Pick"
    options = [
        SelectOption(label=c.name[:100], value=str(c.id), emoji=_champion_emoji(c))
        for c in state.available_champions
    ]
    return [
        ActionRow(
            RouteSelect(
                "pick_champion",
                state.match_id,
                f"{state.step_index}.{index}",
                options=chunk,
                placeholder=f"{placeholder} a champion: {chunk[0].label} to {chunk[-1].label}"[
                    :150
                ],
            )
        )
        for index, chunk in enumerate(_chunks(options))
    ]


def _champion_draft(state: BoardState, box: Container[Any]) -> None:
    box.add_item(TextDisplay(_draft_summary(state)))
    noun = "Ban" if state.step_action == "ban" else "Pick"
    progress = f"{noun} {state.turn_position} of {state.turn_total}." if state.turn_total else ""
    hint = f"{progress}{_next_up(state)}".strip()
    if hint:
        box.add_item(TextDisplay(f"-# {hint}"))


def _random_button(state: BoardState) -> RouteButton:
    return RouteButton(
        "random_champion",
        state.match_id,
        state.step_index,
        label="Random",
        style=ButtonStyle.primary,
        emoji=discord.PartialEmoji(name="🎲"),
    )


def render_pickers(state: BoardState) -> list[LayoutView]:
    """The champion grid, in messages of its own directly below the board.

    They hold nothing else, so they stack into one grid. Each turn they are posted afresh rather
    than edited, so no "(edited)" tag splits the grid.
    """
    if state.status != Status.CHAMPION_DRAFT or state.on_hold:
        return []
    rows = [*_champion_rows(state, state.champions), ActionRow(_random_button(state))]
    if not grid_fits(rows):
        rows = [*_champion_selects(state), ActionRow(_random_button(state))]
    return grid_messages(rows)


def grid_fits(rows: list[ActionRow[Any]]) -> bool:
    return len(rows) <= ROWS_PER_GRID_MESSAGE * GRID_MESSAGES


def grid_messages(rows: list[ActionRow[Any]]) -> list[LayoutView]:
    """Split a grid's rows into its messages, balanced. Also used by /startdraft."""
    messages = -(-len(rows) // ROWS_PER_GRID_MESSAGE)
    per_message = -(-len(rows) // messages)  # Balanced, so neither half looks like an afterthought.
    views = []
    for start in range(0, len(rows), per_message):
        view = LayoutView(timeout=None)
        for row in rows[start : start + per_message]:
            view.add_item(row)
        views.append(view)
    return views


def _lobby_code(state: BoardState, box: Container[Any]) -> None:
    # Bebot's finished draft: the summary, # GLHF and a GIF.
    box.add_item(TextDisplay(_draft_summary(state) + "\n\n# GLHF"))
    if state.gif_url:
        box.add_item(MediaGallery(discord.MediaGalleryItem(state.gif_url)))
    box.add_item(Separator())
    timer = "" if state.on_hold else countdown(state)
    due = f"\n{timer}" if timer else ""
    box.add_item(
        TextDisplay(f"**{state.host_name}** hosts: create the custom lobby.\n{CHECKLIST}{due}")
    )
    box.add_item(
        ActionRow(
            RouteButton(
                "code_open", state.match_id, label="Enter lobby code", style=ButtonStyle.primary
            )
        )
    )


def _in_progress(state: BoardState, box: Container[Any]) -> None:
    box.add_item(TextDisplay(f"### Lobby code: `{state.lobby_code}`"))
    box.add_item(Separator())
    title = bebot.draft_title(state.match_id, state.draft_name)
    box.add_item(TextDisplay(f"### {title}\n{_draft_summary(state)}"))
    box.add_item(Separator())
    box.add_item(TextDisplay("Captains: when the game ends, press the team that won."))
    box.add_item(
        ActionRow(
            *[
                RouteButton(
                    "report_open",
                    state.match_id,
                    team,
                    label=f"{state.team_name(team)} won"[:80],
                    style=ButtonStyle.primary,
                )
                for team in ("A", "B")
            ]
        )
    )
    if state.deadline_at is not None:
        box.add_item(
            TextDisplay(f"-# Report by {_stamp(state.deadline_at, 't')}, or staff will step in.")
        )


def _awaiting(state: BoardState, box: Container[Any]) -> None:
    box.add_item(
        TextDisplay(
            f"**{state.reported_by}** reported **{state.team_name(state.reported_winner)}** won.\n"
            "The other captain can confirm, dispute or add a screenshot. "
            "Silence confirms when the timer runs out."
        )
    )
    box.add_item(
        ActionRow(
            RouteButton("confirm", state.match_id, label="Confirm", style=ButtonStyle.success),
            RouteButton("report_open", state.match_id, label="Add my screenshot"),
            RouteButton("dispute_open", state.match_id, label="Dispute", style=ButtonStyle.danger),
        )
    )


def _disputed(state: BoardState, box: Container[Any]) -> None:
    reported = (
        f"{state.reported_by} reported {state.team_name(state.reported_winner)} won.\n"
        if state.reported_by
        else "No result was reported.\n"
    )
    box.add_item(
        TextDisplay(reported + "Staff: choose the result. Ratings are unchanged until then.")
    )
    box.add_item(
        ActionRow(
            RouteButton(
                "resolve_a",
                state.match_id,
                label=f"{state.team_name('A')} won"[:80],
                style=ButtonStyle.primary,
            ),
            RouteButton(
                "resolve_b",
                state.match_id,
                label=f"{state.team_name('B')} won"[:80],
                style=ButtonStyle.primary,
            ),
            RouteButton("resolve_void", state.match_id, label="Void", style=ButtonStyle.danger),
        )
    )


def _closing(state: BoardState) -> str:
    if state.closes_at is None:
        return ""
    return f"\nThis channel closes <t:{int(state.closes_at.timestamp())}:R>."


def _finished(state: BoardState, box: Container[Any]) -> None:
    if state.status == Status.COMPLETED:
        delta = next(
            (
                abs(state.deltas[player.player_id])
                for player in state.players
                if player.player_id in state.deltas
            ),
            0,
        )
        box.add_item(
            TextDisplay(
                f"**{state.team_name(state.winner)} won** (±{delta}). "
                f"Results are posted.{_closing(state)}"
            )
        )
    else:
        box.add_item(TextDisplay(f"No rating changes.{_closing(state)}"))


_SECTIONS = {
    Status.READY_CHECK: _ready,
    Status.CAPTAIN_PHASE: _captain_vote,
    Status.PLAYER_DRAFT: _player_draft,
    Status.SIDE_SELECT: _side_select,
    Status.CHAMPION_DRAFT: _champion_draft,
    Status.LOBBY_CODE: _lobby_code,
    Status.IN_PROGRESS: _in_progress,
    Status.AWAITING_CONFIRMATION: _awaiting,
    Status.DISPUTED: _disputed,
    Status.COMPLETED: _finished,
    Status.VOIDED: _finished,
}


def _accent(state: BoardState) -> int:
    if state.on_hold:
        return ACCENT_ATTENTION
    # Bebot coloured the draft by the side on the clock, and kept the last side's colour after.
    if state.status == Status.CHAMPION_DRAFT and state.acting_team:
        return bebot.side_colour(state.side_of(state.acting_team))
    if state.status == Status.LOBBY_CODE and state.champion_steps:
        return bebot.side_colour(state.side_of(state.champion_steps[-1][1]))
    if state.status == Status.COMPLETED:
        return ACCENT_DONE
    if state.status == Status.VOIDED:
        return ACCENT_VOID
    if state.status in {Status.DISPUTED, Status.AWAITING_CONFIRMATION}:
        return ACCENT_ATTENTION
    return ACCENT_LIVE


def _sub_buttons(state: BoardState, line: SubLine) -> list[RouteButton]:
    if line.status == SubStatus.AWAITING_APPROVAL:
        return [
            RouteButton(
                "sub_approve", state.match_id, line.id, label="Approve", style=ButtonStyle.success
            ),
            RouteButton(
                "sub_reject", state.match_id, line.id, label="Keep them", style=ButtonStyle.danger
            ),
        ]
    cancel = RouteButton("sub_cancel", state.match_id, line.id, label="Cancel sub")
    if line.offer_name:
        return [cancel]
    name = RouteButton(
        "sub_invite_open", state.match_id, line.id, label="Name a sub", style=ButtonStyle.primary
    )
    if line.name_until is None:
        return [name, cancel]
    return [name, RouteButton("sub_queue", state.match_id, line.id, label="Ask the queue"), cancel]


def _sub_text(state: BoardState, line: SubLine) -> str:
    team = f" ({state.team_name(line.team)})" if line.team else ""
    who = f"**{line.leaving_name}**{team}"
    if line.status == SubStatus.AWAITING_APPROVAL:
        return (
            f"🔁 **{line.requested_by_name}** wants to sub out {who}. "
            f"The other captain can approve or refuse {_stamp(line.deadline_at)}."
        )
    if line.offer_name:
        source = "next in the queue" if line.offer_from_queue else "invited"
        answer = f" {_stamp(line.offer_deadline_at)}" if line.offer_deadline_at else ""
        return f"🔁 {who} is subbing out. Waiting for **{line.offer_name}** ({source}){answer}."
    if line.name_until is not None:
        return (
            f"🔁 {who} needs a sub. Name someone, or the queue is asked {_stamp(line.name_until)}."
        )
    return f"🔁 {who} needs a sub. Nobody in the queue can take it yet, so name someone."


def _subs(state: BoardState, box: Container[Any]) -> None:
    for line in state.subs:
        box.add_item(TextDisplay(_sub_text(state, line)))
        box.add_item(ActionRow(*_sub_buttons(state, line)))


def _strip_buttons(box: Container[Any], start: int) -> None:
    """While paused, the phase's own buttons would only be refused, so the board leaves them out."""
    for item in list(box.children)[start:]:
        if isinstance(item, ActionRow):
            box.remove_item(item)


def render_board(state: BoardState) -> LayoutView:
    box: Container[Any] = Container(accent_colour=_accent(state))
    box.add_item(TextDisplay(_header(state)))
    start = len(box.children)
    _SECTIONS[state.status](state, box)
    # A result can still come in while a sub is on the way: the game may have ended anyway.
    if state.on_hold and state.status != Status.IN_PROGRESS:
        _strip_buttons(box, start)
    if state.subs:
        box.add_item(Separator())
        _subs(state, box)
    if state.can_request_sub:
        box.add_item(ActionRow(RouteButton("sub_open", state.match_id, label="Request a sub")))
    if state.status == Status.READY_CHECK:  # Once is enough: it doesn't change during a match.
        box.add_item(TextDisplay(f"-# Format: {state.mode_name}"))
    view = LayoutView(timeout=None)
    view.add_item(box)
    return view


def render_ready_dm(
    match_id: int, invite_url: str | None, *, heading: str, channel_url: str | None = None
) -> LayoutView:
    """The DM sent to players who weren't in the Waiting Room when their lobby popped."""
    buttons: list[discord.ui.Item[Any]] = []
    if invite_url:
        buttons.append(discord.ui.Button(label="Join voice", url=invite_url))
    buttons.append(RouteButton("ready", match_id, label="Ready", style=ButtonStyle.success))
    if channel_url:
        buttons.append(discord.ui.Button(label="Open match channel", url=channel_url))
    box: Container[Any] = Container(
        TextDisplay(f"## {heading}\nMatch #{match_id}. Join voice, then press Ready."),
        ActionRow(*buttons),
        accent_colour=ACCENT_LIVE,
    )
    view = LayoutView(timeout=None)
    view.add_item(box)
    return view


def render_voice_dm(
    match_id: int,
    invite_url: str | None,
    *,
    heading: str,
    body: str = "",
    channel_url: str | None = None,
    voice_label: str = "Join team voice",
) -> LayoutView:
    """A voice invite with no ready step: teams moving to their own channels, or a sub arriving."""
    intro = f"Match #{match_id}." + (f" {body}" if body else "")
    items: list[discord.ui.Item[Any]] = [TextDisplay(f"## {heading}\n{intro}")]
    links: list[discord.ui.Item[Any]] = []
    if invite_url:
        links.append(discord.ui.Button(label=voice_label, url=invite_url))
    if channel_url:
        links.append(discord.ui.Button(label="Open match channel", url=channel_url))
    if links:
        items.append(ActionRow(*links))
    view = LayoutView(timeout=None)
    view.add_item(Container(*items, accent_colour=ACCENT_LIVE))
    return view


def render_sub_offer_dm(info: OfferInfo, team_name: str) -> LayoutView:
    """Asks someone outside the match to take an open spot."""
    team = f" on **{team_name}**" if team_name else ""
    if info.invited_by:
        intro = f"**{info.invited_by}** asked you to take **{info.leaving_name}**'s spot{team}."
    else:
        intro = f"A spot opened{team}, and you're next in the queue."
    phase = STATUS_TITLES.get(info.status, info.status)
    lines = [
        f"## Can you sub in?\nMatch #{info.match_id} ({phase}). {intro}",
        f"Answer {_stamp(info.deadline_at)}.",
    ]
    if info.status == Status.IN_PROGRESS:
        lines.append("-# The game is already running. You take this team's result.")
    view = LayoutView(timeout=None)
    view.add_item(
        Container(
            TextDisplay("\n".join(lines)),
            ActionRow(
                RouteButton(
                    "sub_accept",
                    info.match_id,
                    info.offer_id,
                    label="I'll sub in",
                    style=ButtonStyle.success,
                ),
                RouteButton("sub_decline", info.match_id, info.offer_id, label="Can't right now"),
            ),
            accent_colour=ACCENT_LIVE,
        )
    )
    return view


def render_offer_answered(match_id: int, text: str) -> LayoutView:
    """What an answered offer turns into, so its buttons can't be pressed again."""
    view = LayoutView(timeout=None)
    view.add_item(
        Container(TextDisplay(f"## Match #{match_id}\n{text}"), accent_colour=ACCENT_VOID)
    )
    return view
