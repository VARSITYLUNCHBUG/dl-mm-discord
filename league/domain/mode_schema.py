"""The draft mode file format.

Every mode runs the same flow: a captain vote, the player draft, a side claim, then the champion
draft with the Hidden King team ("first") banning and picking first. A mode only sets timers, team
size, the player draft order, the ban and pick order and excluded champions. Anything not listed
here is rejected, including simultaneous or blind steps, which the v1 engine does not implement."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Seconds = Annotated[int, Field(ge=5, le=3600)]
TeamRole = Literal["first", "second"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(_Strict):
    source: str
    night_shift_status: Literal["confirmed", "candidate_unconfirmed", "not_night_shift"]


class ReadyCheck(_Strict):
    seconds: Seconds = 180
    backfill: Literal["next_in_queue"] = "next_in_queue"


class Captains(_Strict):
    """Once everyone is ready, players vote for up to two captains. Only players with
    `min_games` completed matches can be voted in, so a newcomer can't be troll-picked. Rating
    breaks ties and fills any gap, and the lower-rated captain makes the first player pick."""

    vote_seconds: Seconds = 45
    # Completed matches in this server, across seasons, before a player can captain.
    min_games: Annotated[int, Field(ge=0, le=500)] = 5
    # A player carrying this many dodge strikes can't captain. None turns the check off.
    max_strikes: Annotated[int, Field(ge=1, le=100)] | None = 3


class PlayerDraft(_Strict):
    order: Annotated[str, Field(pattern=r"^[FS]+$")]
    seconds: Seconds = 45
    on_timeout: Literal["auto_highest_rating", "auto_random"] = "auto_highest_rating"


class Step(_Strict):
    action: Literal["ban", "pick"]
    team: TeamRole
    count: Annotated[int, Field(ge=1, le=6)] = 1
    seconds: Seconds | None = None


class ChampionDraft(_Strict):
    """The captains claim a side, then the Hidden King team ("first") bans and picks first. A
    timed-out ban or pick takes a random champion, and the acting captain can press Random."""

    side_seconds: Seconds = 20  # How long the captains have to claim a side. Then a coin flip.
    seconds: Seconds = 30
    excluded_champion_ids: tuple[int, ...] = ()
    steps: Annotated[tuple[Step, ...], Field(min_length=1)]


class LobbyCode(_Strict):
    seconds: Seconds = 180


class Results(_Strict):
    confirm_window_seconds: Seconds = 300
    reminder_minutes: Annotated[int, Field(ge=1, le=600)] = 45
    staff_escalation_minutes: Annotated[int, Field(ge=1, le=1440)] = 180
    # Someone leaving in the game's first minutes voids it, once the other captain agrees.
    early_leave_minutes: Annotated[int, Field(ge=1, le=30)] = 5
    # How long after the lobby code a captain can still report it. Allows for joining and loading.
    early_leave_report_minutes: Annotated[int, Field(ge=1, le=120)] = 20
    early_leave_answer_seconds: Seconds = 120


class Subs(_Strict):
    """Player-requested substitutes. The match is on hold while a slot is open."""

    approve_seconds: Seconds = 60  # The other captain's window to agree to a captain's request.
    name_seconds: Seconds = 45  # How long whoever opened the slot has to name a sub.
    invite_seconds: Seconds = 60  # How long a named sub has to accept.
    offer_seconds: Seconds = 30  # How long each queue player has to accept, in queue order.
    max_wait_seconds: Seconds = 180  # Unfilled by then, the match is cancelled.


class DraftMode(_Strict):
    schema_version: Literal[1]
    slug: Annotated[str, Field(pattern=r"^[a-z0-9_]+$", max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    provenance: Provenance
    team_size: Annotated[int, Field(ge=2, le=6)] = 6
    ready_check: ReadyCheck = ReadyCheck()
    captains: Captains = Captains()
    player_draft: PlayerDraft
    champion_draft: ChampionDraft
    lobby_code: LobbyCode = LobbyCode()
    results: Results = Results()
    subs: Subs = Subs()

    @property
    def lobby_size(self) -> int:
        return self.team_size * 2

    @property
    def champions_consumed_max(self) -> int:
        """Most champions a draft can use: every ban and pick lands."""
        return sum(step.count for step in self.champion_draft.steps)

    @model_validator(mode="after")
    def _check_player_order(self) -> DraftMode:
        need = self.team_size - 1
        first = self.player_draft.order.count("F")
        second = self.player_draft.order.count("S")
        if first != need or second != need:
            raise ValueError(
                f"player_draft.order: each captain must make {need} picks; "
                f"got F={first}, S={second}"
            )
        return self

    @model_validator(mode="after")
    def _check_champion_picks(self) -> DraftMode:
        for team in ("first", "second"):
            picks = sum(
                step.count
                for step in self.champion_draft.steps
                if step.action == "pick" and step.team == team
            )
            if picks != self.team_size:
                raise ValueError(
                    f"champion_draft.steps: {team} team has {picks} champion picks, "
                    f"needs {self.team_size}"
                )
        return self


class ModeValidationError(ValueError):
    """Readable, path-prefixed problems with a draft mode body."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_mode(body: object) -> DraftMode:
    try:
        return DraftMode.model_validate(body)
    except ValidationError as exc:
        problems = []
        for error in exc.errors():
            path = ".".join(str(part) for part in error["loc"])
            message = error["msg"].removeprefix("Value error, ")
            problems.append(f"{path}: {message}" if path else message)
        raise ModeValidationError(problems) from exc
