"""Pure match rules: legal transitions, the captain vote, timeout choices and a format dry-run.

Nothing here touches Django or Discord, so every rule is testable in isolation. All randomness
comes from `seeded_rng`, so a match's outcomes can be reproduced from its stored seed.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from league.domain.draft_plan import build_plan, team_for_role
from league.domain.errors import InvalidAction
from league.domain.mode_schema import DraftMode, ModeValidationError


class Status:
    READY_CHECK = "ready_check"
    CAPTAIN_PHASE = "captain_phase"
    PLAYER_DRAFT = "player_draft"
    SIDE_SELECT = "side_select"
    CHAMPION_DRAFT = "champion_draft"
    LOBBY_CODE = "lobby_code"
    IN_PROGRESS = "in_progress"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DISPUTED = "disputed"
    COMPLETED = "completed"
    VOIDED = "voided"


TERMINAL = frozenset({Status.COMPLETED, Status.VOIDED})
LIVE = frozenset(
    {
        Status.READY_CHECK,
        Status.CAPTAIN_PHASE,
        Status.PLAYER_DRAFT,
        Status.SIDE_SELECT,
        Status.CHAMPION_DRAFT,
        Status.LOBBY_CODE,
        Status.IN_PROGRESS,
        Status.AWAITING_CONFIRMATION,
        Status.DISPUTED,
    }
)

_TRANSITIONS: dict[str, frozenset[str]] = {
    Status.READY_CHECK: frozenset({Status.CAPTAIN_PHASE, Status.VOIDED}),
    Status.CAPTAIN_PHASE: frozenset({Status.PLAYER_DRAFT, Status.VOIDED}),
    Status.PLAYER_DRAFT: frozenset({Status.SIDE_SELECT, Status.VOIDED}),
    Status.SIDE_SELECT: frozenset({Status.CHAMPION_DRAFT, Status.VOIDED}),
    Status.CHAMPION_DRAFT: frozenset({Status.LOBBY_CODE, Status.VOIDED}),
    Status.LOBBY_CODE: frozenset({Status.IN_PROGRESS, Status.VOIDED}),
    Status.IN_PROGRESS: frozenset({Status.AWAITING_CONFIRMATION, Status.DISPUTED, Status.VOIDED}),
    Status.AWAITING_CONFIRMATION: frozenset({Status.COMPLETED, Status.DISPUTED, Status.VOIDED}),
    Status.DISPUTED: frozenset({Status.COMPLETED, Status.VOIDED}),
    # A completed match can be voided or rescored only through the correction path.
    Status.COMPLETED: frozenset({Status.VOIDED}),
    Status.VOIDED: frozenset({Status.COMPLETED}),
}


def can_transition(current: str, target: str) -> bool:
    return target in _TRANSITIONS.get(current, frozenset())


def seeded_rng(seed: int, purpose: str) -> random.Random:
    """A separate stream per decision, so adding a decision later cannot shift earlier ones."""
    return random.Random(f"{seed}:{purpose}")


@dataclass(frozen=True, slots=True)
class CaptainCandidate:
    player_id: int
    rating: int
    games: int


def tally_captain_votes(
    votes: Mapping[int, int], candidates: Sequence[CaptainCandidate], seed: int
) -> tuple[int, int]:
    """Return (team A captain, team B captain) from a captain vote.

    Most votes first; ties go to higher rating, then more games, then a seeded shuffle. A player
    with no votes ranks on rating alone, so a short vote is filled by Elo.
    """
    if len(candidates) < 2:
        raise ValueError("Need at least two candidates.")
    rng = seeded_rng(seed, "captain-vote")
    ordered_input = sorted(candidates, key=lambda c: c.player_id)
    tiebreak = {c.player_id: rng.random() for c in ordered_input}
    ranked = sorted(
        ordered_input,
        key=lambda c: (-votes.get(c.player_id, 0), -c.rating, -c.games, tiebreak[c.player_id]),
    )
    return ranked[0].player_id, ranked[1].player_id


DRAFT_NAME_ADJECTIVES = (
    "Crimson", "Azure", "Golden", "Silver", "Shadow", "Blazing", "Frozen", "Thunder", "Savage",
    "Mystic", "Ancient", "Furious", "Silent", "Fierce", "Radiant", "Obsidian", "Emerald",
    "Scarlet", "Iron", "Ivory", "Wild", "Grim", "Noble", "Ghost", "Storm", "Feral", "Rogue",
    "Midnight", "Arcane", "Molten", "Verdant", "Crystal", "Vicious", "Hallowed", "Relentless",
    "Burning", "Glacial", "Venomous",
)  # fmt: skip
DRAFT_NAME_NOUNS = (
    "Eagle", "Wolf", "Dragon", "Viper", "Tiger", "Phoenix", "Raven", "Kraken", "Falcon", "Bear",
    "Hydra", "Panther", "Cobra", "Hawk", "Lion", "Jaguar", "Stag", "Fox", "Serpent", "Griffin",
    "Rhino", "Bison", "Stallion", "Hound", "Otter", "Warden", "Marauder", "Sentinel", "Berserker",
    "Reaver", "Juggernaut", "Specter", "Wyrm", "Basilisk", "Leviathan", "Chimera",
)  # fmt: skip


def draft_name(seed: int) -> str:
    """A name for the match's draft, such as "Crimson Wolf". The same seed gives the same name."""
    rng = seeded_rng(seed, "draft-name")
    return f"{rng.choice(DRAFT_NAME_ADJECTIVES)} {rng.choice(DRAFT_NAME_NOUNS)}"


def pick_gif(seed: int, gifs: Sequence[str]) -> str | None:
    if not gifs:
        return None
    return seeded_rng(seed, "draft-gif").choice(list(gifs))


def first_pick_team(rating_a: int, rating_b: int, seed: int) -> str:
    """Which team, A or B, makes the first player pick: the lower-rated captain's. A tie is a
    seeded coin flip."""
    if rating_a == rating_b:
        return coin_flip_team(seed, "first-pick:")
    return "A" if rating_a < rating_b else "B"


def coin_flip_team(seed: int, purpose: str) -> str:
    return seeded_rng(seed, purpose).choice(["A", "B"])


@dataclass(frozen=True, slots=True)
class DraftablePlayer:
    player_id: int
    rating: int


def auto_player_pick(
    undrafted: Sequence[DraftablePlayer], rule: str, seed: int, step_index: int
) -> int:
    """The timeout pick: highest rated (lowest ID breaks ties, so it is reproducible) or random."""
    if not undrafted:
        raise InvalidAction("Nobody is left to draft.")
    if rule == "auto_random":
        ordered = sorted(p.player_id for p in undrafted)
        return seeded_rng(seed, f"auto-player:{step_index}").choice(ordered)
    return min(undrafted, key=lambda p: (-p.rating, p.player_id)).player_id


def auto_champion(available: Sequence[int], seed: int, step_index: int) -> int:
    if not available:
        raise InvalidAction("No champions are left.")
    return seeded_rng(seed, f"auto-champion:{step_index}").choice(sorted(available))


def dry_run(mode: DraftMode, champion_pool: Sequence[int]) -> None:
    """Walk the format's plan and check it can finish with this pool, assuming every ban lands.

    Raises ModeValidationError if it cannot.
    """
    pool = set(champion_pool) - set(mode.champion_draft.excluded_champion_ids)
    if len(pool) < mode.champions_consumed_max:
        raise ModeValidationError(
            [
                f"champion_draft.steps: needs {mode.champions_consumed_max} champions, "
                f"but only {len(pool)} are enabled and not excluded"
            ]
        )
    rosters = {"A": 1, "B": 1}
    picks = {"A": 0, "B": 0}
    for step in build_plan(mode):
        team = team_for_role(step.role, "A")
        if step.action == "player_pick":
            rosters[team] += 1
        elif step.action == "pick":
            picks[team] += 1
    if set(rosters.values()) != {mode.team_size} or set(picks.values()) != {mode.team_size}:
        raise ModeValidationError(
            [f"format does not produce {mode.team_size} players and champions per team"]
        )
