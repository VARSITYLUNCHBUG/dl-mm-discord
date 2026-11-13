"""Reading report screenshots with `scoreocr`, and folding what they say into the match.

Each community picks a mode: `off`, `shadow` (read and store stats, change nothing) or `on`
(also set the MVP and key players, fill a missing match ID and cross-check the winner).

The reader returns names as they appear in game, which are not Discord names. Each team's six
rows are matched to that team's six players by name similarity, with confirmed in-game names
from earlier matches counting as exact. When that isn't clear-cut, the rows stay unmatched and a
captain or staff matches them on the board; their answer is remembered for next time.

The engine is optional: without the `ocr` dependency group, `available()` is False and nothing
here imports it.
"""

from __future__ import annotations

import difflib
import importlib.util
import io
import itertools
import json
import logging
import re
import threading
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from league.domain.engine import Status
from league.domain.errors import InvalidAction, NotAllowed, StaleAction
from league.models import (
    Champion,
    Match,
    MatchParticipant,
    Player,
    PlayerAlias,
    PlayerMatchStat,
    ResultSubmission,
    ScoreboardRead,
)
from league.models.scoreboards import AwardSource, MapSource, ReadStatus
from league.services.common import add_effect, lock_match, now, roster

logger = logging.getLogger(__name__)

ENGINE_VERSION = "scoreocr/1"
MODES = ("off", "shadow", "on")
TEAM_NAMES = {"Hidden King": "hk", "Archmother": "am"}
AUTO_MIN = 0.8  # Every pair in an automatic match scores at least this.
AUTO_MARGIN = 0.1  # And the next-best assignment is at least this much worse in total.
ROW_ACCEPT = 0.9  # A single row is still accepted alone when it is this sure and unambiguous.
ROW_MARGIN = 0.15
STAT_FIELDS = {
    "souls": "souls",
    "k": "kills",
    "d": "deaths",
    "a": "assists",
    "player_dmg": "player_damage",
    "obj_dmg": "objective_damage",
    "healing": "healing",
}


def available() -> bool:
    """Whether the reader and its models can run here."""
    return all(
        importlib.util.find_spec(name) is not None for name in ("numpy", "onnxruntime", "PIL")
    )


_engine: Any = None
_engine_lock = threading.Lock()


def read_image(data: bytes) -> dict[str, Any]:
    """Run the reader on image bytes. Slow: call it off the event loop and the database thread.
    Raises `scoreocr.layout.NotAScoreboard` when there is no scoreboard in the image."""
    global _engine
    import numpy as np
    from PIL import Image

    from scoreocr.engine import Engine

    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        with Image.open(io.BytesIO(data)) as image:
            rgb = np.asarray(image.convert("RGB"))
        result = _engine.read(rgb)
    # Plain JSON types only, so the read can be stored as is: numpy numbers become Python ones.
    return dict(json.loads(json.dumps(result, default=_plain)))


def _plain(value: Any) -> Any:
    item = getattr(value, "item", None)
    return item() if callable(item) else str(value)


def is_not_a_scoreboard(exc: BaseException) -> bool:
    return type(exc).__name__ == "NotAScoreboard"


# --- Parsing ---------------------------------------------------------------------------------


def parse_value(text: object) -> int | None:
    """A displayed value as a number: "12", "38k" or "3.4k". The same rules as the reader's own
    validation, kept here so ingesting needs no OCR dependencies."""
    if not isinstance(text, str):
        return None
    if re.fullmatch(r"\d{1,3}", text, re.ASCII):
        return int(text)
    if re.fullmatch(r"\d{1,3}k", text, re.ASCII):
        return int(text[:-1]) * 1000
    if re.fullmatch(r"\d\.\dk", text, re.ASCII):
        return round(float(text[:-1]) * 1000)
    return None


def normalise(name: str) -> str:
    """Case, accents, spacing and punctuation don't count when comparing names."""
    folded = unicodedata.normalize("NFKD", name).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


@dataclass(frozen=True, slots=True)
class Candidate:
    player_id: int
    display_name: str
    confirmed: frozenset[str]  # Normalised in-game names a captain or staff confirmed.
    seen: frozenset[str]  # Normalised names auto-matched before, not yet confirmed.


def similarity(name: str, candidate: Candidate) -> float:
    key = normalise(name)
    if not key:
        return 0.0
    if key in candidate.confirmed:
        return 1.0
    scores = [difflib.SequenceMatcher(None, key, normalise(candidate.display_name)).ratio()]
    scores += [0.95 * difflib.SequenceMatcher(None, key, alias).ratio() for alias in candidate.seen]
    return max(scores)


@dataclass(frozen=True, slots=True)
class Mapped:
    player_id: int | None
    score: float
    source: str


def map_team(names: Sequence[str], candidates: Sequence[Candidate]) -> list[Mapped]:
    """Match one team's scoreboard names to its players, or leave rows unmatched."""
    if not names or not candidates:
        return [Mapped(None, 0.0, "") for _ in names]
    scores = [[similarity(name, c) for c in candidates] for name in names]
    width = min(len(names), len(candidates))
    rows = list(range(len(names)))

    def total(assignment: tuple[int, ...], picked_rows: tuple[int, ...]) -> float:
        return sum(scores[r][c] for r, c in zip(picked_rows, assignment, strict=True))

    picked_rows = tuple(rows[:width])
    ranked = sorted(
        (
            (total(assignment, picked_rows), assignment)
            for assignment in itertools.permutations(range(len(candidates)), width)
        ),
        reverse=True,
    )
    best_total, best = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    pair_scores = [scores[r][c] for r, c in zip(picked_rows, best, strict=True)]
    clear_cut = min(pair_scores) >= AUTO_MIN and best_total - runner_up >= AUTO_MARGIN
    all_confirmed = all(score == 1.0 for score in pair_scores)
    if len(names) == len(candidates) and (clear_cut or all_confirmed):
        return [
            Mapped(
                candidates[c].player_id,
                scores[r][c],
                MapSource.ALIAS if scores[r][c] == 1.0 else MapSource.AUTO,
            )
            for r, c in zip(picked_rows, best, strict=True)
        ]

    # Not clear as a whole: keep only rows that are sure on their own.
    result = [Mapped(None, 0.0, "") for _ in names]
    taken: set[int] = set()
    for r in rows:
        order = sorted(range(len(candidates)), key=lambda c: -scores[r][c])
        top = order[0]
        second = scores[r][order[1]] if len(order) > 1 else 0.0
        if (
            scores[r][top] >= ROW_ACCEPT
            and scores[r][top] - second >= ROW_MARGIN
            and top not in taken
        ):
            taken.add(top)
            source = MapSource.ALIAS if scores[r][top] == 1.0 else MapSource.AUTO
            result[r] = Mapped(candidates[top].player_id, scores[r][top], source)
    return result


# --- Ingesting a read ------------------------------------------------------------------------


def _team_letters(match: Match) -> dict[str, str]:
    """Scoreboard team name to the match's team key."""
    hk = match.hidden_king_team
    if hk not in {"A", "B"}:
        return {}
    other = "B" if hk == "A" else "A"
    return {"Hidden King": hk, "Archmother": other}


def _candidates(match: Match) -> dict[str, list[Candidate]]:
    rows = roster(match).select_related("player")
    by_team: dict[str, list[int]] = {"A": [], "B": []}
    names: dict[int, str] = {}
    confirmed: dict[int, set[str]] = {}
    seen: dict[int, set[str]] = {}
    for row in rows:
        by_team.setdefault(row.team, []).append(row.player_id)
        names[row.player_id] = row.player.display_name
        confirmed[row.player_id], seen[row.player_id] = set(), set()
        # The scoreboard shows Steam names, so a linked one is the best clue there is.
        steam_name = normalise(row.player.steam_name)
        if steam_name:
            verified = row.player.steam_verified_at is not None
            (confirmed if verified else seen)[row.player_id].add(steam_name)
    aliases = PlayerAlias.objects.filter(player_id__in=list(names))
    for player_id, name_norm, is_confirmed in aliases.values_list(
        "player_id", "name_norm", "confirmed"
    ):
        (confirmed if is_confirmed else seen)[player_id].add(name_norm)
    return {
        team: [
            Candidate(p, names[p], frozenset(confirmed[p]), frozenset(seen[p])) for p in sorted(ids)
        ]
        for team, ids in by_team.items()
    }


def _hero(name: str | None, heroes: dict[str, Champion]) -> Champion | None:
    if not name:
        return None
    return heroes.get(normalise(name))


def _upsert_alias(player_id: int, name: str, *, confirmed: bool) -> None:
    key = normalise(name)
    if not key:
        return
    alias = PlayerAlias.objects.filter(player_id=player_id, name_norm=key).first()
    if alias is None:
        PlayerAlias.objects.create(
            player_id=player_id, name=name[:100], name_norm=key[:100], confirmed=confirmed,
            last_seen_at=now(),
        )  # fmt: skip
        return
    alias.times_seen += 1
    alias.last_seen_at = now()
    alias.confirmed = alias.confirmed or confirmed
    alias.save(update_fields=["times_seen", "last_seen_at", "confirmed"])


def _badges_clean(result: dict[str, Any]) -> bool:
    badges = [p.get("badge") for t in result.get("teams", []) for p in t.get("players", [])]
    return badges.count("mvp") == 1 and badges.count("key") == 2


def _status(read_problems: list[str], unmapped: int) -> str:
    if unmapped:
        return ReadStatus.NEEDS_MAPPING
    if any(not problem.startswith("soft:") for problem in read_problems):
        return ReadStatus.NEEDS_REVIEW
    return ReadStatus.OK


@transaction.atomic
def ingest(
    submission_id: int, result: dict[str, Any], engine_version: str = ENGINE_VERSION
) -> ScoreboardRead:
    """Store a read and its rows, match names to players, then act on it if the mode is `on`."""
    submission = ResultSubmission.objects.get(pk=submission_id)
    match = lock_match(submission.match_id)
    letters = _team_letters(match)
    candidates = _candidates(match)
    heroes = {normalise(c.name): c for c in Champion.objects.all()}
    problems = [str(p) for p in result.get("needs_review", [])]
    winner_name = result.get("winner")
    read = ScoreboardRead.objects.create(
        match=match,
        submission=submission,
        image_sha256=submission.image_sha256,
        status=ReadStatus.OK,
        engine_version=engine_version[:32],
        raw=result,
        problems=problems,
        ocr_winner=letters.get(str(winner_name), "") if winner_name else "",
        ocr_match_id=result.get("match_id") if isinstance(result.get("match_id"), int) else None,
        duration_s=result.get("duration_s") if isinstance(result.get("duration_s"), int) else None,
        badges_clean=_badges_clean(result),
        winner_clean=bool(winner_name) and bool(letters.get(str(winner_name))),
        created_at=now(),
    )
    unmapped = 0
    row_number = 0
    for team_block in result.get("teams", [])[:2]:
        team = letters.get(str(team_block.get("name")), "")
        players = list(team_block.get("players", []))[:6]
        mapped = map_team([str(p.get("name") or "") for p in players], candidates.get(team, []))
        for player, mapping in zip(players, mapped, strict=True):
            hero = _hero(player.get("hero"), heroes)
            stat = PlayerMatchStat(
                read=read,
                match=match,
                row=row_number,
                player_id=mapping.player_id,
                team=team,
                ocr_name=str(player.get("name") or "")[:100],
                hero=hero,
                hero_name=str(player.get("hero") or "")[:60],
                badge=str(player.get("badge") or "")[:8],
                confidence=float(player.get("confidence") or 0),
                map_score=round(mapping.score, 3),
                map_source=mapping.source,
            )
            for key, attr in STAT_FIELDS.items():
                setattr(stat, attr, parse_value(player.get(key)))
            stat.save()
            if mapping.player_id is None:
                unmapped += 1
            elif mapping.source == MapSource.AUTO:
                _upsert_alias(mapping.player_id, stat.ocr_name, confirmed=False)
            row_number += 1
    read.status = _status(problems, unmapped)
    read.save(update_fields=["status"])
    act_on(read)
    if unmapped and match.status not in {Status.VOIDED}:
        add_effect(
            match.community,
            "scoreboard_notice",
            {"read_id": read.pk, "unmapped": unmapped},
            match=match,
            dedupe_key=f"scoreboard_notice:{read.pk}",
        )
    return read


def record_failure(submission_id: int, status: str, reason: str) -> ScoreboardRead:
    submission = ResultSubmission.objects.get(pk=submission_id)
    return ScoreboardRead.objects.create(
        match_id=submission.match_id,
        submission=submission,
        image_sha256=submission.image_sha256,
        status=status,
        engine_version=ENGINE_VERSION,
        problems=[reason[:500]],
        created_at=now(),
    )


def submission_image(submission_id: int) -> tuple[int, int, int] | None:
    """Where the archived screenshot is: (channel, message, attachment)."""
    row = ResultSubmission.objects.filter(pk=submission_id).first()
    if row is None or not row.archive_channel_id or not row.archive_message_id:
        return None
    return row.archive_channel_id, row.archive_message_id, row.attachment_id or 0


# --- Acting on a read ------------------------------------------------------------------------


def _claimed_winner(match: Match) -> str:
    if match.status == Status.COMPLETED:
        return match.winner
    report = match.submissions.filter(verdict="report").order_by("id").first()
    return report.claimed_winner if report else ""


def act_on(read: ScoreboardRead) -> None:
    """In `on` mode: cross-check the winner, fill the match ID, and set the awards when the read
    can be trusted. Called again after names are matched by hand."""
    from league.services import corrections, results

    match = Match.objects.select_related("community", "season").get(pk=read.match_id)
    if match.community.score_ocr != "on":
        return
    claimed = _claimed_winner(match)
    if read.winner_clean and read.ocr_winner and claimed and read.ocr_winner != claimed:
        if match.status == Status.AWAITING_CONFIRMATION:
            results._dispute(match, None, "The scoreboard shows the other team won.")
        elif match.status == Status.COMPLETED:
            add_effect(
                match.community,
                "ping_staff",
                {"text": "is recorded the other way from what its scoreboard shows."},
                match=match,
                dedupe_key=f"ocr_winner_mismatch:{match.pk}",
            )

    submission = read.submission
    if submission.deadlock_match_id is None and read.ocr_match_id:
        submission.deadlock_match_id = read.ocr_match_id
        submission.save(update_fields=["deadlock_match_id"])
    if read.ocr_match_id:
        from league.services import api_results

        api_results.set_deadlock_match_id(match, read.ocr_match_id)

    if not read.badges_clean or match.status not in {
        Status.AWAITING_CONFIRMATION,
        Status.DISPUTED,
        Status.COMPLETED,
    }:
        return
    holders = list(read.stats.exclude(badge=""))
    if len(holders) != 3 or any(stat.player_id is None for stat in holders):
        return
    awards = {stat.player_id: stat.badge for stat in holders if stat.player_id is not None}
    corrections.apply_award_correction(
        match.pk, awards, AwardSource.OCR, None, "Read from the scoreboard"
    )


# --- Matching names by hand ------------------------------------------------------------------


def latest_read(match: Match) -> ScoreboardRead | None:
    return (
        ScoreboardRead.objects.filter(match=match)
        .exclude(status__in=(ReadStatus.REJECTED, ReadStatus.FAILED))
        .order_by("-id")
        .first()
    )


@dataclass(frozen=True, slots=True)
class UnmappedRow:
    row: int
    ocr_name: str
    hero_name: str
    team: str
    options: list[tuple[int, str]]  # (player ID, name) of that team's players not yet matched.


def unmapped_rows(read_id: int) -> list[UnmappedRow]:
    read = ScoreboardRead.objects.select_related("match").get(pk=read_id)
    stats = list(read.stats.all())
    taken = {stat.player_id for stat in stats if stat.player_id is not None}
    by_team: dict[str, list[tuple[int, str]]] = {}
    for participant in roster(read.match).select_related("player"):
        if participant.player_id not in taken:
            by_team.setdefault(participant.team, []).append(
                (participant.player_id, participant.player.display_name)
            )
    return [
        UnmappedRow(stat.row, stat.ocr_name, stat.hero_name, stat.team, by_team.get(stat.team, []))
        for stat in stats
        if stat.player_id is None
    ]


def can_map(read_id: int, player: Player, is_staff: bool) -> None:
    read = ScoreboardRead.objects.select_related("match").filter(pk=read_id).first()
    if read is None:
        raise StaleAction("That scoreboard read no longer exists.")
    if is_staff:
        return
    captain = MatchParticipant.objects.filter(
        match=read.match, player=player, is_captain=True, removed_reason=""
    ).exists()
    if not captain:
        raise NotAllowed("Only a captain or staff can match scoreboard names.")


@transaction.atomic
def map_row(read_id: int, row: int, player_id: int, actor: Player, *, is_staff: bool) -> int:
    """Match one scoreboard row to a player. Returns how many rows are still unmatched."""
    read = ScoreboardRead.objects.get(pk=read_id)
    lock_match(read.match_id)
    can_map(read_id, actor, is_staff)
    stat = read.stats.filter(row=row).first()
    if stat is None:
        raise StaleAction("That row isn't on the scoreboard.")
    on_team = roster(read.match).filter(player_id=player_id, team=stat.team).exists()
    if not on_team:
        raise InvalidAction("That player wasn't on that team.")
    if read.stats.filter(player_id=player_id).exclude(pk=stat.pk).exists():
        raise InvalidAction("That player is already matched to another row.")
    stat.player_id = player_id
    stat.map_source = MapSource.STAFF if is_staff else MapSource.CAPTAIN
    stat.map_score = 1.0
    stat.save(update_fields=["player", "map_source", "map_score"])
    _upsert_alias(player_id, stat.ocr_name, confirmed=True)
    left = read.stats.filter(player__isnull=True).count()
    if left == 0:
        read.status = _status(list(read.problems), 0)
        read.save(update_fields=["status"])
        act_on(read)
    return left
