"""A match's MVP and key players: who decided them, and which decision stands.

Awards are append-only revisions. A later revision replaces the set, but only if its source
ranks at least as high as the current one: staff, then deadlock-api, then the scoreboard, then a
captain's report. So a confident read replaces what a captain picked, the game's own data replaces
a read, and nothing but staff replaces staff.
"""

from __future__ import annotations

from collections.abc import Mapping

from django.db.models import Max

from league.domain.errors import InvalidAction
from league.models import Match, MatchAward, Player
from league.models.scoreboards import AwardKind, AwardSource
from league.services.common import now, roster

RANK: dict[str, int] = {
    AwardSource.REPORT: 1,
    AwardSource.OCR: 2,
    AwardSource.API: 3,
    AwardSource.STAFF: 4,
}
MAX_MVP = 1
MAX_KEY = 2


def current(match: Match) -> tuple[dict[int, str], str]:
    """(player ID to kind, source) of the revision in force. Source is "" before any."""
    latest = MatchAward.objects.filter(match=match).aggregate(latest=Max("revision"))["latest"]
    if latest is None:
        return {}, ""
    rows = list(MatchAward.objects.filter(match=match, revision=latest))
    source = rows[0].source if rows else ""
    return {row.player_id: row.kind for row in rows if row.player_id is not None}, source


def validate(match: Match, awards: Mapping[int, str]) -> None:
    on_roster = set(roster(match).values_list("player_id", flat=True))
    if any(player_id not in on_roster for player_id in awards):
        raise InvalidAction("Awards can only go to players on the match's final roster.")
    kinds = list(awards.values())
    if any(kind not in AwardKind.values for kind in kinds):
        raise InvalidAction("An award is either MVP or key player.")
    if kinds.count(AwardKind.MVP) > MAX_MVP:
        raise InvalidAction("A match has one MVP.")
    if kinds.count(AwardKind.KEY) > MAX_KEY:
        raise InvalidAction(f"A match has at most {MAX_KEY} key players.")


def record(
    match: Match, awards: Mapping[int, str], source: str, actor: Player | None
) -> int | None:
    """Append a revision and return its number. None when nothing changes: the set is the same,
    or a higher-ranked source already decided."""
    validate(match, awards)
    existing, existing_source = current(match)
    if existing_source and RANK[source] < RANK[existing_source]:
        return None
    if existing_source and dict(awards) == existing:
        return None
    if not existing_source and not awards:
        return None
    revision = (
        MatchAward.objects.filter(match=match).aggregate(latest=Max("revision"))["latest"] or 0
    ) + 1
    moment = now()
    rows = [
        MatchAward(
            match=match,
            revision=revision,
            player_id=player_id,
            kind=kind,
            source=source,
            actor=actor,
            created_at=moment,
        )
        for player_id, kind in sorted(awards.items())
    ]
    if not rows:  # Clearing the awards still needs a row to carry the revision.
        rows = [
            MatchAward(
                match=match, revision=revision, source=source, actor=actor, created_at=moment
            )
        ]
    MatchAward.objects.bulk_create(rows)
    return revision


def from_picks(mvp: int | None, keys: list[int]) -> dict[int, str]:
    """Build an award set from pickers. The MVP wins if someone is picked as both."""
    awards = dict.fromkeys(keys[:MAX_KEY], AwardKind.KEY)
    if mvp is not None:
        awards[mvp] = AwardKind.MVP
    return {player_id: str(kind) for player_id, kind in awards.items()}
