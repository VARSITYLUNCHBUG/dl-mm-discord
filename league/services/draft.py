"""The player draft, champion draft and lobby code. One step per plan entry, one row per step."""

from __future__ import annotations

import re

from django.db import IntegrityError, transaction

from league.domain.draft_plan import PlanStep, build_plan, player_draft_length, team_for_role
from league.domain.engine import (
    DraftablePlayer,
    Status,
    auto_champion,
    auto_player_pick,
    coin_flip_team,
    seeded_rng,
)
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.domain.mode_schema import DraftMode
from league.models import DraftAction, Match, Player
from league.models.matches import Source
from league.services.common import (
    active_participants,
    add_effect,
    bump,
    captain_of,
    clear_notices,
    lock_match,
    mode_of,
    now,
    rating_of,
    render_board,
    require_not_on_hold,
    save_transition,
    seconds_from_now,
    transition,
)

LOBBY_CODE_PATTERN = re.compile(r"^[A-Za-z0-9-]{3,32}$")


def _current_step(match: Match, mode: DraftMode, step_index: int | None) -> PlanStep:
    plan = build_plan(mode)
    if step_index is not None and step_index != match.step_index:
        raise StaleAction("Too late: that step has already been taken.")
    if match.step_index >= len(plan):
        raise StaleAction("The draft has finished.")
    return plan[match.step_index]


def _acting_team(match: Match, step: PlanStep) -> str:
    first = match.player_first_pick if step.action == "player_pick" else match.champion_first_pick
    return team_for_role(step.role, first)


def _record(
    match: Match,
    step: PlanStep,
    team: str,
    *,
    actor: Player | None,
    source: str,
    target_player_id: int | None = None,
    target_champion_id: int | None = None,
    interaction_id: int | None = None,
    random_choice: bool = False,
) -> None:
    try:
        with transaction.atomic():
            DraftAction.objects.create(
                match=match,
                step_index=step.index,
                action=step.action,
                team=team,
                actor=actor,
                target_player_id=target_player_id,
                target_champion_id=target_champion_id,
                source=source,
                random_choice=random_choice,
                interaction_id=interaction_id,
                created_at=now(),
            )
    except IntegrityError as exc:
        raise StaleAction("Too late: that step has already been taken.") from exc


def _undrafted(match: Match) -> list[DraftablePlayer]:
    players = []
    for participant in active_participants(match).filter(team=""):
        rating, _games = rating_of(match.season, participant.player_id)
        players.append(DraftablePlayer(participant.player_id, rating))
    return players


def _assign_player(match: Match, step: PlanStep, team: str, player_id: int) -> None:
    participant = active_participants(match).filter(player_id=player_id, team="").first()
    if participant is None:
        raise InvalidAction("That player has already been drafted.")
    participant.team = team
    participant.draft_pick_number = step.index + 1
    participant.save(update_fields=["team", "draft_pick_number"])


def _require_acting_captain(match: Match, team: str, player: Player) -> None:
    if captain_of(match, team).player_id != player.pk:
        raise NotAllowed("It's not your turn.")


# --- Player draft ----------------------------------------------------------------------------


@transaction.atomic
def pick_player(
    match_id: int,
    captain: Player,
    step_index: int,
    target_player_id: int,
    interaction_id: int | None = None,
) -> None:
    match = lock_match(match_id)
    if match.status != Status.PLAYER_DRAFT:
        raise StaleAction("The player draft has finished.")
    require_not_on_hold(match)
    mode = mode_of(match)
    step = _current_step(match, mode, step_index)
    team = _acting_team(match, step)
    _require_acting_captain(match, team, captain)
    _assign_player(match, step, team, target_player_id)
    _record(
        match,
        step,
        team,
        actor=captain,
        source=Source.USER,
        target_player_id=target_player_id,
        interaction_id=interaction_id,
    )
    _advance_player_draft(match, mode)


def _advance_player_draft(match: Match, mode: DraftMode) -> None:
    match.step_index += 1
    if match.step_index >= player_draft_length(mode):
        begin_side_select(match, mode)
        return
    remaining = _undrafted(match)
    if len(remaining) == 1:
        # Only one player left, so there is nothing to choose.
        step = build_plan(mode)[match.step_index]
        team = _acting_team(match, step)
        _assign_player(match, step, team, remaining[0].player_id)
        _record(
            match,
            step,
            team,
            actor=None,
            source=Source.SYSTEM,
            target_player_id=remaining[0].player_id,
        )
        _advance_player_draft(match, mode)
        return
    match.deadline_at = seconds_from_now(mode.player_draft.seconds)
    bump(match, "step_index", "deadline_at")
    render_board(match)


def on_player_draft_timeout(match: Match) -> None:
    mode = mode_of(match)
    step = _current_step(match, mode, None)
    team = _acting_team(match, step)
    target = auto_player_pick(
        _undrafted(match), mode.player_draft.on_timeout, match.rng_seed, step.index
    )
    _assign_player(match, step, team, target)
    _record(match, step, team, actor=None, source=Source.TIMEOUT, target_player_id=target)
    _advance_player_draft(match, mode)


# --- Champion draft --------------------------------------------------------------------------


SIDES = {"hk": "Hidden King", "am": "The Archmother"}


def begin_side_select(match: Match, mode: DraftMode) -> None:
    """The player draft is over: make team voice channels and have the captains claim a side."""
    transition(
        match,
        Status.SIDE_SELECT,
        source=Source.SYSTEM,
        deadline_at=seconds_from_now(mode.champion_draft.side_seconds),
    )
    save_transition(match, "step_index")
    add_effect(
        match.community, "create_team_voice", match=match, dedupe_key=f"team_voice:{match.pk}"
    )
    captain_ids = [captain_of(match, team).player.discord_user_id for team in ("A", "B")]
    add_effect(
        match.community,
        "ping",
        {
            "user_ids": captain_ids,
            "text": "teams are set. Claim a side: Hidden King bans and picks first.",
        },
        match=match,
    )
    render_board(match)


@transaction.atomic
def claim_side(match_id: int, captain: Player, side: str) -> None:
    """The first captain to claim a side decides both."""
    match = lock_match(match_id)
    if match.status != Status.SIDE_SELECT:
        raise StaleAction("Sides have already been chosen.")
    require_not_on_hold(match)
    if side not in SIDES:
        raise InvalidAction("That isn't a side.")
    participant = match.participants.filter(player=captain, is_active=True, is_captain=True).first()
    if participant is None:
        raise NotAllowed("Only a captain can claim a side.")
    other = "B" if participant.team == "A" else "A"
    match.hidden_king_team = participant.team if side == "hk" else other
    begin_champion_draft(match, mode_of(match), source=Source.USER, actor=captain)


def on_side_timeout(match: Match) -> None:
    match.hidden_king_team = coin_flip_team(match.rng_seed, "side")
    begin_champion_draft(match, mode_of(match), source=Source.TIMEOUT)


def begin_champion_draft(
    match: Match, mode: DraftMode, *, source: str, actor: Player | None = None
) -> None:
    """Sides are set. The Hidden King team bans and picks first."""
    match.champion_first_pick = match.hidden_king_team
    step = build_plan(mode)[match.step_index]
    transition(
        match,
        Status.CHAMPION_DRAFT,
        source=source,
        actor=actor,
        deadline_at=seconds_from_now(step.seconds),
    )
    save_transition(match, "champion_first_pick", "hidden_king_team", "step_index")
    hidden_king = captain_of(match, match.hidden_king_team).player
    add_effect(
        match.community,
        "ping",
        {
            "user_ids": [hidden_king.discord_user_id],
            "text": "you're Hidden King. You ban first. The champion draft is on.",
        },
        match=match,
    )
    render_board(match)


def _used_champions(match: Match) -> set[int]:
    return set(
        match.draft_actions.filter(target_champion__isnull=False).values_list(
            "target_champion_id", flat=True
        )
    )


def available_champions(match: Match) -> list[int]:
    used = _used_champions(match)
    return [champion for champion in match.champion_pool if champion not in used]


@transaction.atomic
def choose_champion(
    match_id: int,
    captain: Player,
    step_index: int,
    champion_id: int,
    interaction_id: int | None = None,
) -> None:
    match = lock_match(match_id)
    if match.status != Status.CHAMPION_DRAFT:
        raise StaleAction("The champion draft has finished.")
    require_not_on_hold(match)
    mode = mode_of(match)
    step = _current_step(match, mode, step_index)
    team = _acting_team(match, step)
    _require_acting_captain(match, team, captain)
    if champion_id not in available_champions(match):
        raise InvalidAction("That champion isn't available.")
    _record(
        match,
        step,
        team,
        actor=captain,
        source=Source.USER,
        target_champion_id=champion_id,
        interaction_id=interaction_id,
    )
    _advance_champion_draft(match, mode)


@transaction.atomic
def choose_random_champion(
    match_id: int,
    captain: Player,
    step_index: int,
    interaction_id: int | None = None,
) -> int:
    """The Random button: the acting captain takes a random available champion. Returns it."""
    match = lock_match(match_id)
    if match.status != Status.CHAMPION_DRAFT:
        raise StaleAction("The champion draft has finished.")
    require_not_on_hold(match)
    mode = mode_of(match)
    step = _current_step(match, mode, step_index)
    team = _acting_team(match, step)
    _require_acting_captain(match, team, captain)
    champion = auto_champion(available_champions(match), match.rng_seed, step.index)
    _record(
        match,
        step,
        team,
        actor=captain,
        source=Source.USER,
        target_champion_id=champion,
        interaction_id=interaction_id,
        random_choice=True,
    )
    _advance_champion_draft(match, mode)
    return champion


def on_champion_draft_timeout(match: Match) -> None:
    mode = mode_of(match)
    step = _current_step(match, mode, None)
    team = _acting_team(match, step)
    champion = auto_champion(available_champions(match), match.rng_seed, step.index)
    _record(match, step, team, actor=None, source=Source.TIMEOUT, target_champion_id=champion)
    _advance_champion_draft(match, mode)


def _advance_champion_draft(match: Match, mode: DraftMode) -> None:
    plan = build_plan(mode)
    match.step_index += 1
    if match.step_index >= len(plan):
        begin_lobby_code(match, mode)
        return
    match.deadline_at = seconds_from_now(plan[match.step_index].seconds)
    bump(match, "step_index", "deadline_at")
    render_board(match)


# --- Lobby code ------------------------------------------------------------------------------


def begin_lobby_code(match: Match, mode: DraftMode) -> None:
    team = seeded_rng(match.rng_seed, "lobby-host").choice(["A", "B"])
    host = captain_of(match, team)
    match.lobby_code_host = host.player
    match.lobby_code_attempts = 1
    transition(
        match,
        Status.LOBBY_CODE,
        source=Source.SYSTEM,
        deadline_at=seconds_from_now(mode.lobby_code.seconds),
    )
    save_transition(match, "lobby_code_host", "lobby_code_attempts", "step_index")
    _ping_host(match, host.player)
    render_board(match)


def _ping_host(match: Match, host: Player) -> None:
    add_effect(
        match.community,
        "ping",
        {
            "user_ids": [host.discord_user_id],
            "text": "create the custom lobby, then press Enter lobby code on the board.",
        },
        match=match,
    )


def on_lobby_code_timeout(match: Match) -> None:
    from league.services.lobby import void_live  # Avoids a cycle: lobby calls into the draft flow.

    mode = mode_of(match)
    if match.lobby_code_attempts >= 2:
        void_live(match, source=Source.TIMEOUT, reason="No lobby code was provided")
        return
    current_team = match.participants.get(player=match.lobby_code_host).team
    other = captain_of(match, "B" if current_team == "A" else "A")
    match.lobby_code_host = other.player
    match.lobby_code_attempts = 2
    match.deadline_at = seconds_from_now(mode.lobby_code.seconds)
    bump(match, "lobby_code_host", "lobby_code_attempts", "deadline_at")
    clear_notices(match)  # The first host's prompt no longer applies.
    _ping_host(match, other.player)
    render_board(match)


def check_lobby_host(match_id: int, player: Player) -> None:
    """Fast check before opening the modal. The submission checks again under the lock."""
    match = Match.objects.filter(pk=match_id).first()
    if match is None or match.status != Status.LOBBY_CODE:
        raise StaleAction("The lobby code step has finished.")
    require_not_on_hold(match)
    if match.lobby_code_host_id != player.pk:
        raise NotAllowed("Only the chosen host can enter the lobby code.")


@transaction.atomic
def submit_lobby_code(match_id: int, player: Player, code: str) -> None:
    match = lock_match(match_id)
    if match.status != Status.LOBBY_CODE:
        raise StaleAction("The lobby code step has finished.")
    require_not_on_hold(match)
    if match.lobby_code_host_id != player.pk:
        raise NotAllowed("Only the chosen host can enter the lobby code.")
    cleaned = code.strip()
    if not LOBBY_CODE_PATTERN.match(cleaned):
        raise InvalidAction("That doesn't look like a lobby code: use letters, numbers or dashes.")
    mode = mode_of(match)
    match.lobby_code = cleaned
    match.started_at = now()
    transition(
        match,
        Status.IN_PROGRESS,
        source=Source.USER,
        actor=player,
        deadline_at=seconds_from_now(mode.results.staff_escalation_minutes * 60),
    )
    save_transition(match, "lobby_code", "started_at")
    add_effect(match.community, "announce_lobby_code", match=match)
    add_effect(
        match.community,
        "report_reminder",
        match=match,
        dedupe_key=f"reminder:{match.pk}",
        run_after=seconds_from_now(mode.results.reminder_minutes * 60),
    )
    render_board(match)
