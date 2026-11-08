"""Read models for what the bot displays. Plain dataclasses, so renderers need no database."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from league.domain.draft_plan import PlanStep, build_plan, team_for_role
from league.domain.engine import TERMINAL, Status, draft_name, pick_gif
from league.domain.mode_schema import parse_mode
from league.models import (
    Champion,
    Community,
    Match,
    Player,
    PlayerRating,
    QueueEntry,
    RatingEvent,
    ResultSubmission,
    Season,
)
from league.models.matches import HandoffStatus
from league.models.subs import UNRESOLVED, OfferSource, OfferStatus, SubRequest, SubStatus
from league.services.common import mode_of, rating_of
from league.services.gifs import draft_gifs
from league.services.lobby import TEXT_CHANNEL_GRACE_MINUTES
from league.services.ratings import match_deltas
from league.services.subs import SUBBABLE


def team_name(captain: str) -> str:
    """The only name players ever see for a team. A and B stay internal."""
    return f"{captain}'s Team"


def captain_names(match: Match) -> dict[str, str]:
    """Team key to captain name, for a match with captains on its roster."""
    rows = match.participants.filter(is_captain=True, removed_reason="").exclude(team="")
    return dict(rows.values_list("team", "player__display_name"))


def outcome_label(match: Match, outcome: str) -> str:
    """Words for a stored outcome: a team's name, or "void"."""
    name = captain_names(match).get(outcome)
    return team_name(name) if name else outcome


@dataclass(frozen=True, slots=True)
class PlayerLine:
    player_id: int
    discord_user_id: int
    name: str
    rating: int
    team: str = ""
    is_captain: bool = False
    is_ready: bool = False
    confirmed_captain: bool = False


@dataclass(frozen=True, slots=True)
class HandoffLine:
    id: int
    team: str
    from_name: str
    to_name: str
    to_discord_user_id: int


@dataclass(frozen=True, slots=True)
class DraftLine:
    action: str
    team: str
    label: str
    auto: bool
    random_choice: bool = False


@dataclass(frozen=True, slots=True)
class SubLine:
    """One sub request still in play, as the board shows it."""

    id: int
    status: str
    leaving_name: str
    team: str
    requested_by_name: str
    is_self: bool
    deadline_at: datetime
    name_until: datetime | None = None
    offer_name: str = ""
    offer_from_queue: bool = False
    offer_deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChampionOption:
    id: int
    name: str
    emoji_id: int | None = None
    taken: bool = False  # Banned or picked. It stays on the grid, so nothing shifts.


@dataclass(slots=True)
class BoardState:
    match_id: int
    status: str
    mode_name: str
    is_candidate_format: bool
    deadline_at: datetime | None
    step_index: int
    players: list[PlayerLine]
    handoffs: list[HandoffLine] = field(default_factory=list)
    handoffs_left: dict[str, int] = field(default_factory=dict)
    actions: list[DraftLine] = field(default_factory=list)
    acting_team: str = ""
    acting_captain_name: str = ""
    step_action: str = ""
    steps_total: int = 0
    turn_position: int = 0
    turn_total: int = 0
    champions: list[ChampionOption] = field(default_factory=list)
    hidden_king_team: str = ""
    host_name: str = ""
    lobby_code: str = ""
    winner: str = ""
    reported_winner: str = ""
    reported_by: str = ""
    deltas: dict[int, int] = field(default_factory=dict)
    is_terminal: bool = False
    captains: dict[str, str] = field(default_factory=dict)
    closes_at: datetime | None = None
    subs: list[SubLine] = field(default_factory=list)
    on_hold: bool = False
    can_request_sub: bool = False
    # Who acts after this step, when it isn't more of the same.
    next_captain_name: str = ""
    next_action: str = ""
    # The captain vote: whether this match votes, and who has voted so far.
    captain_vote: bool = False
    voted_ids: frozenset[int] = frozenset()
    # Bebot touches: a name for the draft, a GIF for its summary, and a Random button.
    draft_name: str = ""
    gif_url: str | None = None
    random_button: bool = False
    hidden_king_first: bool = False
    ban_timeout_skips: bool = True

    @property
    def hold_until(self) -> datetime | None:
        """When the match is cancelled if no sub has come in."""
        open_rows = [line.deadline_at for line in self.subs if line.status == SubStatus.OPEN]
        return min(open_rows) if open_rows else None

    @property
    def available_champions(self) -> list[ChampionOption]:
        return [champion for champion in self.champions if not champion.taken]

    def team_name(self, team: str) -> str:
        return team_name(self.captains.get(team, "Captain"))


def _turn_progress(plan: tuple[PlanStep, ...], index: int) -> tuple[int, int]:
    """Where this step sits: within a captain's run of player picks, or among all bans or picks."""
    step = plan[index]
    if step.action != "player_pick":
        same = [other.index for other in plan if other.action == step.action]
        return same.index(index) + 1, len(same)

    def same_captain(other: PlanStep) -> bool:
        return other.action == "player_pick" and other.role == step.role

    start = index
    while start > 0 and same_captain(plan[start - 1]):
        start -= 1
    end = index
    while end + 1 < len(plan) and same_captain(plan[end + 1]):
        end += 1
    return index - start + 1, end - start + 1


def board_state(match_id: int) -> BoardState:
    match = Match.objects.select_related("mode_version", "season", "lobby_code_host").get(
        pk=match_id
    )
    mode = mode_of(match)
    participants = match.participants.filter(removed_reason="").select_related("player")
    players = []
    for participant in participants:
        rating, _games = rating_of(match.season, participant.player_id)
        players.append(
            PlayerLine(
                player_id=participant.player_id,
                discord_user_id=participant.player.discord_user_id,
                name=participant.player.display_name,
                rating=rating,
                team=participant.team,
                is_captain=participant.is_captain,
                is_ready=participant.ready_at is not None,
                confirmed_captain=participant.captain_confirmed_at is not None,
            )
        )
    players.sort(key=lambda line: (-line.rating, line.name.lower()))

    state = BoardState(
        match_id=match.pk,
        status=match.status,
        mode_name=mode.name,
        is_candidate_format=mode.provenance.night_shift_status == "candidate_unconfirmed",
        deadline_at=match.deadline_at,
        step_index=match.step_index,
        players=players,
        hidden_king_team=match.hidden_king_team,
        lobby_code=match.lobby_code,
        winner=match.winner,
        is_terminal=match.status in TERMINAL,
    )
    state.captains = {p.team: p.name for p in players if p.is_captain and p.team}
    if match.ended_at is not None:
        state.closes_at = match.ended_at + timedelta(minutes=TEXT_CHANNEL_GRACE_MINUTES)
    if match.lobby_code_host is not None:
        state.host_name = match.lobby_code_host.display_name

    limit = mode.captains.handoff.requests_per_captain
    for team in ("A", "B"):
        state.handoffs_left[team] = limit - match.handoffs.filter(team=team).count()
    pending = match.handoffs.filter(status=HandoffStatus.PENDING).select_related(
        "from_player", "to_player"
    )
    state.handoffs = [
        HandoffLine(
            id=handoff.pk,
            team=handoff.team,
            from_name=handoff.from_player.display_name,
            to_name=handoff.to_player.display_name,
            to_discord_user_id=handoff.to_player.discord_user_id,
        )
        for handoff in pending
    ]

    champion_names = dict(Champion.objects.values_list("pk", "name"))
    actions = match.draft_actions.select_related("target_player").order_by("step_index")
    for action in actions:
        if action.target_player is not None:
            label = action.target_player.display_name
        elif action.target_champion_id is not None:
            label = champion_names.get(action.target_champion_id, str(action.target_champion_id))
        else:
            label = "skipped"
        state.actions.append(
            DraftLine(
                action.action, action.team, label, action.source == "timeout", action.random_choice
            )
        )

    plan = build_plan(mode)
    state.steps_total = len(plan)
    if match.status in {Status.PLAYER_DRAFT, Status.CHAMPION_DRAFT} and match.step_index < len(
        plan
    ):
        step = plan[match.step_index]
        first = (
            match.player_first_pick if step.action == "player_pick" else match.champion_first_pick
        )
        state.step_action = step.action
        state.acting_team = team_for_role(step.role, first)
        captains = [p for p in players if p.is_captain and p.team == state.acting_team]
        state.acting_captain_name = captains[0].name if captains else ""
        state.turn_position, state.turn_total = _turn_progress(plan, match.step_index)
        _next_turn(state, plan, match, players)
    if match.status == Status.CHAMPION_DRAFT:
        used = set(
            match.draft_actions.filter(target_champion__isnull=False).values_list(
                "target_champion_id", flat=True
            )
        )
        rows = Champion.objects.filter(pk__in=match.champion_pool).order_by("name")
        state.champions = [
            ChampionOption(row.pk, row.name, row.discord_emoji_id, row.pk in used) for row in rows
        ]

    report = match.submissions.filter(verdict="report").select_related("captain").first()
    if report is not None:
        state.reported_winner = report.claimed_winner
        state.reported_by = report.captain.display_name
    if match.status == Status.COMPLETED:
        state.deltas = match_deltas(match)
    state.captain_vote = mode.captains.selection == "vote"
    if state.captain_vote and match.status == Status.CAPTAIN_PHASE:
        active = {line.player_id for line in players}
        state.voted_ids = frozenset(
            match.captain_votes.filter(voter_id__in=active).values_list("voter_id", flat=True)
        )
    state.draft_name = draft_name(match.rng_seed)
    state.gif_url = pick_gif(match.rng_seed, draft_gifs())
    state.random_button = mode.champion_draft.random_button
    state.hidden_king_first = mode.champion_draft.priority == "hidden_king_first"
    state.ban_timeout_skips = mode.champion_draft.on_timeout.ban == "skip"
    state.on_hold = match.held_seconds is not None
    state.can_request_sub = match.status in SUBBABLE
    state.subs = _sub_lines(match)
    return state


def _next_turn(
    state: BoardState, plan: tuple[PlanStep, ...], match: Match, players: list[PlayerLine]
) -> None:
    """Name who goes next, unless it's the same captain doing the same again."""
    step = plan[match.step_index]
    if match.step_index + 1 >= len(plan):
        return
    upcoming = plan[match.step_index + 1]
    if (upcoming.action == "player_pick") != (step.action == "player_pick"):
        return  # The next step belongs to the next phase.
    first = (
        match.player_first_pick if upcoming.action == "player_pick" else match.champion_first_pick
    )
    team = team_for_role(upcoming.role, first)
    if team == state.acting_team and upcoming.action == step.action:
        return
    captain = next((p for p in players if p.is_captain and p.team == team), None)
    if captain is not None:
        state.next_captain_name = captain.name
        state.next_action = upcoming.action


def _sub_lines(match: Match) -> list[SubLine]:
    rows = (
        SubRequest.objects.filter(match=match, status__in=UNRESOLVED)
        .select_related("leaving", "requested_by")
        .order_by("id")
    )
    teams = dict(match.participants.filter(is_active=True).values_list("player_id", "team"))
    lines = []
    for row in rows:
        offer = row.offers.filter(status=OfferStatus.PENDING).select_related("player").first()
        lines.append(
            SubLine(
                id=row.pk,
                status=row.status,
                leaving_name=row.leaving.display_name,
                team=teams.get(row.leaving_id, ""),
                requested_by_name=row.requested_by.display_name,
                is_self=row.is_self,
                deadline_at=row.deadline_at,
                name_until=row.name_until,
                offer_name=offer.player.display_name if offer else "",
                offer_from_queue=bool(offer and offer.source == OfferSource.QUEUE),
                offer_deadline_at=offer.deadline_at if offer else None,
            )
        )
    return lines


@dataclass(frozen=True, slots=True)
class QueueView:
    size: int
    capacity: int
    players: list[str]
    mode_name: str
    live_matches: int
    waiting_room_channel_id: int | None = None


def queue_view(community: Community) -> QueueView:
    mode = parse_mode(community.active_mode.body)
    entries = QueueEntry.objects.filter(community=community).select_related("player")
    return QueueView(
        size=entries.count(),
        capacity=mode.lobby_size,
        players=[entry.player.display_name for entry in entries],
        mode_name=mode.name,
        live_matches=Match.objects.filter(community=community).exclude(status__in=TERMINAL).count(),
        waiting_room_channel_id=community.waiting_room_channel_id,
    )


@dataclass(frozen=True, slots=True)
class ResultRow:
    player_id: int
    discord_user_id: int
    name: str
    before: int
    after: int
    is_captain: bool

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass(slots=True)
class ResultsView:
    match_id: int
    winner: str
    hidden_king_team: str
    expected_a: float
    delta_a: int
    avg_a: float
    avg_b: float
    team_a: list[ResultRow]
    team_b: list[ResultRow]
    champions_a: list[str]
    champions_b: list[str]
    bans: list[str]
    screenshot: ResultSubmission | None
    captains: dict[str, str] = field(default_factory=dict)

    def team_name(self, team: str) -> str:
        return team_name(self.captains.get(team, "Captain"))


def results_view(match_id: int) -> ResultsView:
    match = Match.objects.select_related("season").get(pk=match_id)
    events = {
        event.player_id: event
        for event in RatingEvent.objects.filter(match=match, run__kind="live").select_related("run")
    }
    team_a: list[ResultRow] = []
    team_b: list[ResultRow] = []
    participants = match.participants.filter(removed_reason="").select_related("player")
    for participant in participants:
        event = events.get(participant.player_id)
        after = event.rating_after if event else match.season.start_rating
        before = after - event.delta if event else after
        row = ResultRow(
            player_id=participant.player_id,
            discord_user_id=participant.player.discord_user_id,
            name=participant.player.display_name,
            before=before,
            after=after,
            is_captain=participant.is_captain,
        )
        (team_a if participant.team == "A" else team_b).append(row)
    team_a.sort(key=lambda r: -r.before)
    team_b.sort(key=lambda r: -r.before)

    sample_a = next((events[r.player_id] for r in team_a if r.player_id in events), None)
    names = dict(Champion.objects.values_list("pk", "name"))
    champions: dict[str, list[str]] = {"A": [], "B": []}
    bans: list[str] = []
    for action in match.draft_actions.filter(target_champion__isnull=False).order_by("step_index"):
        champion_id = action.target_champion_id
        if champion_id is None:
            continue
        name = names.get(champion_id, str(champion_id))
        if action.action == "pick":
            champions[action.team].append(name)
        elif action.action == "ban":
            bans.append(name)
    screenshot = match.submissions.filter(archive_message_id__isnull=False).order_by("id").first()
    return ResultsView(
        match_id=match.pk,
        winner=match.winner,
        hidden_king_team=match.hidden_king_team,
        expected_a=sample_a.expected if sample_a and sample_a.expected else 0.5,
        delta_a=sample_a.delta if sample_a else 0,
        avg_a=sample_a.team_avg if sample_a and sample_a.team_avg else 0.0,
        avg_b=sample_a.opp_avg if sample_a and sample_a.opp_avg else 0.0,
        team_a=team_a,
        team_b=team_b,
        champions_a=champions["A"],
        champions_b=champions["B"],
        bans=bans,
        screenshot=screenshot,
        captains=captain_names(match),
    )


@dataclass(frozen=True, slots=True)
class CorrectionLine:
    discord_user_id: int
    name: str
    net: int
    rating: int


@dataclass(frozen=True, slots=True)
class CorrectionView:
    match_id: int
    summary: str
    reason: str
    lines: list[CorrectionLine]
    more: int


def correction_view(
    match_id: int, run_id: int | None, outcome: str, reason: str, limit: int = 20
) -> CorrectionView:
    """Net rating changes from one correction, across every match it touched."""
    match = Match.objects.get(pk=match_id)
    if outcome in {"A", "B"}:
        summary = f"{outcome_label(match, outcome)} won"
    else:
        summary = {"void": "voided", "sub": "roster changed"}.get(outcome, "updated")
    lines: list[CorrectionLine] = []
    more = 0
    if run_id is not None:
        net: dict[tuple[int, str], int] = {}
        events = RatingEvent.objects.filter(run_id=run_id).select_related("player")
        latest: dict[tuple[int, str], int] = {}
        for event in events.order_by("id"):
            key = (event.player.discord_user_id, event.player.display_name)
            net[key] = net.get(key, 0) + event.delta
            latest[key] = event.rating_after
        ordered = sorted(
            (
                CorrectionLine(uid, name, total, latest[(uid, name)])
                for (uid, name), total in net.items()
                if total
            ),
            key=lambda line: -abs(line.net),
        )
        lines = ordered[:limit]
        more = max(0, len(ordered) - limit)
    return CorrectionView(match_id, summary, reason, lines, more)


@dataclass(frozen=True, slots=True)
class LeaderboardRow:
    rank: int
    name: str
    rating: int
    wins: int
    losses: int


def leaderboard(season: Season, limit: int = 20) -> list[LeaderboardRow]:
    rows = (
        PlayerRating.objects.filter(season=season, games__gte=season.leaderboard_min_games)
        .select_related("player")
        .order_by("-rating", "player__display_name")[:limit]
    )
    return [
        LeaderboardRow(index + 1, row.player.display_name, row.rating, row.wins, row.losses)
        for index, row in enumerate(rows)
    ]


@dataclass(frozen=True, slots=True)
class PlayerStats:
    name: str
    rating: int
    games: int
    wins: int
    losses: int
    rank: int | None
    recent: list[tuple[int, int, int]]  # (match ID, delta, rating after)


def player_stats(season: Season, player: Player, history: int = 10) -> PlayerStats:
    row = PlayerRating.objects.filter(season=season, player=player).first()
    rating = row.rating if row else season.start_rating
    games = row.games if row else 0
    rank = None
    if row and games >= season.leaderboard_min_games:
        rank = (
            PlayerRating.objects.filter(
                season=season, games__gte=season.leaderboard_min_games, rating__gt=rating
            ).count()
            + 1
        )
    recent = RatingEvent.objects.filter(season=season, player=player).order_by("-id")[:history]
    return PlayerStats(
        name=player.display_name,
        rating=rating,
        games=games,
        wins=row.wins if row else 0,
        losses=row.losses if row else 0,
        rank=rank,
        recent=[(event.match_id or 0, event.delta, event.rating_after) for event in recent],
    )
