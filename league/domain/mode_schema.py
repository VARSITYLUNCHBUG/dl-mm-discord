"""The draft mode file format. Anything not listed here is rejected, including simultaneous or
blind steps, which the v1 engine does not implement."""

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


class Handoff(_Strict):
    requests_per_captain: Annotated[int, Field(ge=0, le=5)] = 2
    accept_seconds: Seconds = 30


class Captains(_Strict):
    # "vote": once everyone is ready, players vote for up to two captains. Elo fills any gaps.
    selection: Literal["top_rating", "random", "vote"] = "top_rating"
    min_games: Annotated[int, Field(ge=0)] = 0
    first_pick: Literal["lower_rated_captain", "higher_rated_captain", "coin_flip"] = (
        "lower_rated_captain"
    )
    phase_seconds: Seconds = 20  # The keep-or-hand-off window, or how long the vote lasts.
    handoff: Handoff = Handoff()


class PlayerDraft(_Strict):
    order: Annotated[str, Field(pattern=r"^[FS]+$")]
    seconds: Seconds = 45
    on_timeout: Literal["auto_highest_rating", "auto_random"] = "auto_highest_rating"


class Step(_Strict):
    action: Literal["ban", "pick"]
    team: TeamRole
    count: Annotated[int, Field(ge=1, le=6)] = 1
    seconds: Seconds | None = None


class OnTimeout(_Strict):
    ban: Literal["skip", "auto_random"] = "skip"
    pick: Literal["auto_random"] = "auto_random"


class ChampionDraft(_Strict):
    # "coin_flip": first pick and side are flipped separately. "hidden_king_first": the captains
    # claim a side, and the Hidden King team bans and picks first.
    priority: Literal["coin_flip", "hidden_king_first"] = "coin_flip"
    side_seconds: Seconds = 20  # How long the captains have to claim a side.
    seconds: Seconds = 30
    excluded_champion_ids: tuple[int, ...] = ()
    on_timeout: OnTimeout = OnTimeout()
    random_button: bool = False  # Lets the acting captain take a random champion.
    steps: Annotated[tuple[Step, ...], Field(min_length=1)]


class LobbyCode(_Strict):
    seconds: Seconds = 180


class Results(_Strict):
    confirm_window_seconds: Seconds = 300
    reminder_minutes: Annotated[int, Field(ge=1, le=600)] = 45
    staff_escalation_minutes: Annotated[int, Field(ge=1, le=1440)] = 180


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
    def _check_vote_has_no_handoff(self) -> DraftMode:
        if self.captains.selection == "vote" and self.captains.handoff.requests_per_captain:
            raise ValueError(
                "captains.handoff.requests_per_captain: must be 0 when captains are voted in"
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
