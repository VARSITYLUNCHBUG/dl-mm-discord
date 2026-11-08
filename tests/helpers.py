"""Drive a match through its phases using only public service calls."""

from __future__ import annotations

from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import Status
from league.models import Community, Match, Player
from league.services import draft, lobby, queue, results
from league.services.common import captain_of, mode_of
from league.services.results import ImageRef


def image(number: int = 1) -> ImageRef:
    return ImageRef(
        sha256=f"{number:064x}",
        archive_channel_id=1,
        archive_message_id=1000 + number,
        attachment_id=2000 + number,
    )


def fresh(match: Match) -> Match:
    return Match.objects.select_related("mode_version", "season", "community").get(pk=match.pk)


def ready_all(match: Match) -> Match:
    match = fresh(match)
    if match.status == Status.READY_CHECK:
        for participant in match.participants.filter(is_active=True, ready_at__isnull=True):
            lobby.mark_ready(match.pk, participant.player)
    return fresh(match)


def keep_captains(match: Match) -> Match:
    """Settle the captain phase: keep the captains, or, in a vote, vote for the first two."""
    match = fresh(match)
    if match.status == Status.CAPTAIN_PHASE and lobby.is_vote(match):
        active = list(match.participants.filter(is_active=True).order_by("id"))
        chosen = [active[0].player_id, active[1].player_id]
        for participant in active:
            if fresh(match).status != Status.CAPTAIN_PHASE:
                break
            lobby.cast_captain_votes(match.pk, participant.player, chosen)
    elif match.status == Status.CAPTAIN_PHASE:
        for participant in match.participants.filter(is_active=True, is_captain=True):
            lobby.keep_captaincy(match.pk, participant.player)
    return fresh(match)


def claim_sides(match: Match, side: str = "hk") -> Match:
    """Team A's captain claims a side, if the format asks captains to."""
    match = fresh(match)
    if match.status == Status.SIDE_SELECT:
        draft.claim_side(match.pk, captain_of(match, "A").player, side)
    return fresh(match)


def run_player_draft(match: Match) -> Match:
    match = fresh(match)
    while match.status == Status.PLAYER_DRAFT:
        step = build_plan(mode_of(match))[match.step_index]
        team = team_for_role(step.role, match.player_first_pick)
        captain = captain_of(match, team).player
        target = match.participants.filter(is_active=True, team="").order_by("id").first()
        draft.pick_player(match.pk, captain, match.step_index, target.player_id)
        match = fresh(match)
    return match


def run_champion_draft(match: Match) -> Match:
    match = fresh(match)
    while match.status == Status.CHAMPION_DRAFT:
        step = build_plan(mode_of(match))[match.step_index]
        team = team_for_role(step.role, match.champion_first_pick)
        captain = captain_of(match, team).player
        champion = draft.available_champions(match)[0]
        draft.choose_champion(match.pk, captain, match.step_index, champion)
        match = fresh(match)
    return match


def to_in_progress(match: Match) -> Match:
    match = run_champion_draft(claim_sides(run_player_draft(keep_captains(ready_all(match)))))
    draft.submit_lobby_code(match.pk, match.lobby_code_host, "ABC123")
    return fresh(match)


def finish(match: Match, winner: str = "A", number: int = 1) -> Match:
    """Report and confirm a result, from a match already in progress."""
    match = fresh(match)
    first = captain_of(match, "A").player
    second = captain_of(match, "B").player
    results.submit_report(match.pk, first, winner, image(number))
    results.confirm(match.pk, second)
    return fresh(match)


def play_full_match(community: Community, players: list[Player], winner: str, number: int) -> Match:
    for player in players:
        queue.join(community.pk, player)
    match = Match.objects.filter(community=community).order_by("-id").first()
    return finish(to_in_progress(match), winner, number)
