"""Checking results against deadlock-api, by Deadlock match ID.

Once a match has a Deadlock match ID (from the API lobby, a captain's report or the scoreboard)
and a result has been reported, its metadata is fetched. deadlock-api only has a custom match once
someone's client has sent it in, so a miss is retried with backoff for a few hours.

When the metadata arrives:

- **Winner.** Team 0 is Hidden King. A different winner disputes an unconfirmed result, or asks
  staff about a confirmed one. Nothing is overturned automatically.
- **Roster.** Each linked Steam account should be on its player's team. A linked account seen on
  the right team becomes verified; one on the other team, or absent, goes to staff.
- **Awards.** The game's `mvp_rank` 1, 2 and 3 are the MVP and the two key players. When all three
  are linked players on the roster, they become the match's awards.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction

from league.domain.engine import Status
from league.models import ApiMatchCheck, Match, MatchParticipant, Player
from league.models.api import CheckStatus
from league.models.scoreboards import AwardSource
from league.services import steam
from league.services.common import add_effect, lock_match, now, roster

logger = logging.getLogger(__name__)

# Minutes to wait before each fetch. About four hours in all.
BACKOFF_MINUTES = (0, 2, 5, 15, 30, 60, 60, 60)
CHECKABLE = frozenset({Status.AWAITING_CONFIRMATION, Status.DISPUTED, Status.COMPLETED})
HIDDEN_KING_TEAM = 0  # In match metadata, team 0 is Hidden King and team 1 the Archmother.


def schedule(match: Match, attempt: int = 1) -> bool:
    """Queue a fetch, if the match has an ID, a result to check and the server wants checks."""
    if not match.community.api_results or match.deadlock_match_id is None:
        return False
    if match.status not in CHECKABLE or attempt > len(BACKOFF_MINUTES):
        return False
    add_effect(
        match.community,
        "check_match_api",
        {"attempt": attempt, "deadlock_match_id": match.deadlock_match_id},
        match=match,
        dedupe_key=f"check_match_api:{match.pk}:{match.deadlock_match_id}:{attempt}",
        run_after=now() + timedelta(minutes=BACKOFF_MINUTES[attempt - 1]),
    )
    return True


def set_deadlock_match_id(match: Match, deadlock_match_id: int) -> bool:
    """Record Deadlock's match ID, first one wins, and check it once there's a result. The caller
    holds the match lock."""
    if match.deadlock_match_id is not None or deadlock_match_id <= 0:
        return False
    match.deadlock_match_id = deadlock_match_id
    match.version += 1
    match.save(update_fields=["deadlock_match_id", "version"])
    schedule(match)
    return True


# --- Reading metadata ------------------------------------------------------------------------


def summarise(data: dict[str, Any]) -> dict[str, Any]:
    """The few fields kept from a 1 MB metadata response."""
    info = data.get("match_info") or {}
    players = []
    for player in info.get("players") or []:
        if not isinstance(player, dict) or "account_id" not in player:
            continue
        stats = (player.get("stats") or [{}])[-1] or {}
        players.append(
            {
                "account_id": int(player["account_id"]),
                "team": player.get("team"),
                "hero_id": player.get("hero_id"),
                "kills": player.get("kills"),
                "deaths": player.get("deaths"),
                "assists": player.get("assists"),
                "net_worth": player.get("net_worth"),
                "player_damage": stats.get("player_damage"),
                "healing": stats.get("player_healing"),
                "mvp_rank": player.get("mvp_rank"),
                # Seconds into the match the player abandoned it. 0 or missing: they didn't.
                "abandoned_s": player.get("abandon_match_time_s") or 0,
            }
        )
    return {
        "match_id": info.get("match_id"),
        "duration_s": info.get("duration_s"),
        "winning_team": info.get("winning_team"),
        "players": players,
    }


def _team_letters(match: Match) -> dict[int, str]:
    hk = match.hidden_king_team
    if hk not in {"A", "B"}:
        return {}
    return {HIDDEN_KING_TEAM: hk, 1 - HIDDEN_KING_TEAM: "B" if hk == "A" else "A"}


def _claimed_winner(match: Match) -> str:
    if match.status == Status.COMPLETED:
        return match.winner
    report = match.submissions.filter(verdict="report").order_by("id").first()
    return report.claimed_winner if report else ""


def _next_attempt(match: Match) -> int:
    return ApiMatchCheck.objects.filter(match=match).count() + 1


@transaction.atomic
def record_miss(match_id: int, deadlock_match_id: int, status: str, http_status: int | None,
                reason: str) -> ApiMatchCheck:  # fmt: skip
    """Not in deadlock-api yet, or the request failed: note it and try again later."""
    match = lock_match(match_id)
    attempt = _next_attempt(match)
    check = ApiMatchCheck.objects.create(
        match=match,
        deadlock_match_id=deadlock_match_id,
        attempt=attempt,
        status=status,
        http_status=http_status,
        problems=[reason[:300]] if reason else [],
        created_at=now(),
    )
    schedule(match, attempt + 1)
    return check


@transaction.atomic
def record_metadata(match_id: int, deadlock_match_id: int, data: dict[str, Any]) -> ApiMatchCheck:
    """Compare deadlock-api's match with ours, store the comparison, and act on it."""
    from league.services import corrections, results

    match = lock_match(match_id)
    summary = summarise(data)
    letters = _team_letters(match)
    api_players = {p["account_id"]: p for p in summary["players"]}
    api_winner = (
        letters.get(summary["winning_team"], "") if summary["winning_team"] is not None else ""
    )

    rows = list(roster(match).select_related("player"))
    linked = {row.player.steam_account_id: row for row in rows if row.player.steam_account_id}
    wrong_team: list[int] = []
    missing: list[int] = []
    seen_right: list[int] = []
    for account_id, row in linked.items():
        found = api_players.get(int(account_id))
        if found is None:
            missing.append(row.player_id)
        elif letters.get(found["team"]) != row.team:
            wrong_team.append(row.player_id)
        else:
            seen_right.append(row.player_id)
    unlinked = sorted(a for a in api_players if a not in linked)
    _note_abandons(match, api_players, linked)

    problems = []
    claimed = _claimed_winner(match)
    if api_winner and claimed and api_winner != claimed:
        problems.append("deadlock-api shows the other team won")
    if linked and not seen_right and len(missing) == len(linked):
        problems.append(f"none of the linked players were in Deadlock match {deadlock_match_id}")
    if wrong_team:
        problems.append(f"{len(wrong_team)} linked player(s) were on the other team")
    status = CheckStatus.MISMATCH if problems else CheckStatus.OK
    check = ApiMatchCheck.objects.create(
        match=match,
        deadlock_match_id=deadlock_match_id,
        attempt=_next_attempt(match),
        status=status,
        http_status=200,
        api_winner=api_winner,
        duration_s=summary["duration_s"] if isinstance(summary["duration_s"], int) else None,
        wrong_team=wrong_team,
        missing=missing,
        unlinked_accounts=unlinked,
        summary=summary,
        problems=problems,
        created_at=now(),
    )
    if status == CheckStatus.OK:
        steam.mark_verified(seen_right)
        awards = _awards(api_players, linked)
        if awards and match.status in CHECKABLE:
            corrections.apply_award_correction(
                match.pk, awards, AwardSource.API, None, "From deadlock-api's match data"
            )
        return check

    text = f"doesn't match deadlock-api ({deadlock_match_id}): {'; '.join(problems)}."
    if (
        api_winner
        and claimed
        and api_winner != claimed
        and match.status == Status.AWAITING_CONFIRMATION
    ):
        results._dispute(
            match, None, f"deadlock-api shows the other team won ({deadlock_match_id})."
        )
    else:
        add_effect(
            match.community,
            "ping_staff",
            {"text": text},
            match=match,
            dedupe_key=f"api_mismatch:{match.pk}:{deadlock_match_id}",
        )
    return check


def _note_abandons(
    match: Match, api_players: dict[int, dict[str, Any]], linked: dict[int, MatchParticipant]
) -> None:
    """Linked players deadlock-api shows abandoning the match are marked as having left early.
    An abandon inside the early-leave window is put to staff, who can void it."""
    from league.services.common import mode_of

    window = mode_of(match).results.early_leave_minutes * 60
    for account_id, row in linked.items():
        abandoned = int(api_players.get(int(account_id), {}).get("abandoned_s") or 0)
        if abandoned <= 0:
            continue
        MatchParticipant.objects.filter(pk=row.pk).update(left_early=True)
        if abandoned <= window and match.status in CHECKABLE:
            minutes, seconds = divmod(abandoned, 60)
            add_effect(
                match.community,
                "ping_staff",
                {
                    "text": f"has {row.player.display_name} abandoning at {minutes}:{seconds:02d}, "
                    "per deadlock-api. If that's an early leave, `/match early_leave` voids it."
                },
                match=match,
                dedupe_key=f"api_abandon:{match.pk}:{row.player_id}",
            )


def _awards(
    api_players: dict[int, dict[str, Any]], linked: dict[int, MatchParticipant]
) -> dict[int, str]:
    """mvp_rank 1 is the MVP and 2 and 3 the key players, when every one is a linked player."""
    ranked = {p.get("mvp_rank"): account for account, p in api_players.items() if p.get("mvp_rank")}
    if not all(rank in ranked for rank in (1, 2, 3)):
        return {}
    picks: dict[int, str] = {}
    for rank, kind in ((1, "mvp"), (2, "key"), (3, "key")):
        row = linked.get(ranked[rank])
        if row is None:
            return {}
        picks[row.player_id] = kind
    return picks


def latest_ok(match: Match) -> ApiMatchCheck | None:
    return (
        ApiMatchCheck.objects.filter(match=match, status__in=(CheckStatus.OK, CheckStatus.MISMATCH))
        .order_by("-id")
        .first()
    )


def api_stat_lines(match: Match) -> dict[int, str]:
    """Per player, a line from deadlock-api's data, for linked players: "Mirage 12/8/13, 38k"."""
    from league.models import Champion
    from league.services.boards import _compact

    check = latest_ok(match)
    if check is None:
        return {}
    heroes = dict(Champion.objects.values_list("pk", "name"))
    by_account = {p["account_id"]: p for p in check.summary.get("players", [])}
    lines = {}
    accounts = dict(
        Player.objects.filter(pk__in=roster(match).values("player_id"))
        .exclude(steam_account_id__isnull=True)
        .values_list("pk", "steam_account_id")
    )
    for player_id, account_id in accounts.items():
        p = by_account.get(account_id)
        if p is None:
            continue
        hero = heroes.get(p.get("hero_id"), "")
        kda = f"{p.get('kills', '?')}/{p.get('deaths', '?')}/{p.get('assists', '?')}"
        lines[player_id] = f"{hero} {kda}, {_compact(p.get('player_damage'))} damage".strip()
    return lines
