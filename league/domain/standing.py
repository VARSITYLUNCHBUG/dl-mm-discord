"""A player's standing: dodge strikes and whether they may captain. Pure, so it can be replayed.

Strikes are a fold over facts, never a stored counter. An offence adds the strikes recorded on its
penalty. Clean completed matches work them off, a few games per strike, and so does time without
an offence. A void or a staff waiver changes the facts, and the next fold reflects it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

EntryKind = Literal["offence", "clean", "adjust"]

# Order of entries at the same instant: an offence lands before a clean game finishing with it.
_KIND_ORDER = {"offence": 0, "adjust": 1, "clean": 2}


def _int(value: object, default: int) -> int:
    return int(value) if isinstance(value, int | float | str) and str(value).strip() else default


@dataclass(frozen=True, slots=True)
class StrikePolicy:
    points: Mapping[str, int]
    games_per_strike: int = 3
    decay_days: int = 14
    max_strikes: int = 10

    @classmethod
    def from_policy(cls, policy: Mapping[str, object]) -> StrikePolicy:
        points = policy.get("strikes", {})
        return cls(
            points=dict(points) if isinstance(points, Mapping) else {},
            games_per_strike=max(1, _int(policy.get("games_per_strike"), 3)),
            decay_days=max(1, _int(policy.get("decay_days"), 14)),
            max_strikes=max(1, _int(policy.get("max_strikes"), 10)),
        )

    def points_for(self, kind: str) -> int:
        return max(0, int(self.points.get(kind, 0)))


@dataclass(frozen=True, slots=True)
class StrikeEntry:
    at: datetime
    kind: EntryKind
    points: int = 0  # Strikes an offence adds, or a staff adjustment's signed delta.
    tiebreak: int = 0  # A stable order for entries at the same instant, such as a row ID.


@dataclass(frozen=True, slots=True)
class Standing:
    strikes: int
    progress: int  # Clean games played towards removing the next strike.
    games_to_next: int | None  # None when there is nothing to work off.
    last_offence_at: datetime | None
    next_decay_at: datetime | None

    @property
    def clean(self) -> bool:
        return self.strikes == 0


CLEAN = Standing(0, 0, None, None, None)


def fold_strikes(entries: Iterable[StrikeEntry], policy: StrikePolicy, at: datetime) -> Standing:
    """Replay a player's offences, clean games and staff adjustments up to `at`."""
    period = timedelta(days=policy.decay_days)
    strikes = 0
    progress = 0
    clock: datetime | None = None  # When the current decay period started.
    last_offence: datetime | None = None

    def decay_until(moment: datetime) -> None:
        nonlocal strikes, clock, progress
        while strikes > 0 and clock is not None and clock + period <= moment:
            strikes -= 1
            clock += period
        if strikes == 0:
            progress = 0

    ordered = sorted(
        (entry for entry in entries if entry.at <= at),
        key=lambda entry: (entry.at, _KIND_ORDER[entry.kind], entry.tiebreak),
    )
    for entry in ordered:
        decay_until(entry.at)
        if entry.kind == "offence":
            if entry.points <= 0:
                continue
            strikes = min(policy.max_strikes, strikes + entry.points)
            progress = 0
            clock = entry.at
            last_offence = entry.at
        elif entry.kind == "clean":
            if strikes == 0:
                continue
            progress += 1
            if progress >= policy.games_per_strike:
                strikes -= 1
                progress = 0
        else:
            was_clean = strikes == 0
            strikes = max(0, min(policy.max_strikes, strikes + entry.points))
            if was_clean and strikes > 0:
                clock = entry.at
            if strikes == 0:
                progress = 0
    decay_until(at)

    if strikes == 0:
        return Standing(0, 0, None, last_offence, None)
    return Standing(
        strikes=strikes,
        progress=progress,
        games_to_next=policy.games_per_strike - progress,
        last_offence_at=last_offence,
        next_decay_at=clock + period if clock is not None else None,
    )


def ladder_rung(strikes_before: int, ladder_length: int) -> int:
    """Which rung of a kind's cooldown ladder applies, zero-based. A clean record is rung 0; every
    strike already carried, from any kind of offence, moves one rung up."""
    if ladder_length <= 0:
        return 0
    return min(max(0, strikes_before), ladder_length - 1)


# --- Captains --------------------------------------------------------------------------------

Override = Literal["allow", "bar", ""]


@dataclass(frozen=True, slots=True)
class CaptainRule:
    min_games: int = 5
    max_strikes: int | None = 3


@dataclass(frozen=True, slots=True)
class Eligibility:
    ok: bool
    reason: str  # Empty when eligible. Short enough for a board line: "2/5 matches".
    games: int


def captain_eligibility(games: int, strikes: int, override: str, rule: CaptainRule) -> Eligibility:
    """Whether a player may captain. Staff "allow" skips the checks; "bar" always blocks."""
    if override == "bar":
        return Eligibility(False, "barred by staff", games)
    if override == "allow":
        return Eligibility(True, "", games)
    if games < rule.min_games:
        return Eligibility(False, f"{games}/{rule.min_games} matches", games)
    if rule.max_strikes is not None and strikes >= rule.max_strikes:
        return Eligibility(False, f"{strikes} strikes", games)
    return Eligibility(True, "", games)
