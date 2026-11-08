"""Random sequences of clicks, double clicks, timeouts, restarts and voids.

Whatever order these arrive in, a match must never break its invariants or get stuck.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone
from hypothesis import HealthCheck, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from hypothesis.strategies import integers, sampled_from

from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import LIVE, Status
from league.domain.errors import DomainError
from league.models import DraftAction, Match, Player, QueueEntry
from league.services import corrections, deadlines, draft, integrity, lobby, queue, results
from league.services.common import captain_of, mode_of
from tests.conftest import Clock, make_player
from tests.helpers import image, keep_captains, run_champion_draft, run_player_draft


class MatchMachine(RuleBasedStateMachine):
    """Runs inside a savepoint per example, so each example starts clean and fast."""

    community_id: int
    clock: Clock
    statuses_seen: set[str] = set()  # noqa: RUF012

    def __init__(self) -> None:
        super().__init__()
        self.savepoint = transaction.savepoint()
        self.players = [make_player(n) for n in range(1, 16)]
        for player in self.players[:12]:
            queue.join(self.community_id, player)
        self.match = Match.objects.order_by("-id").first()
        self.image_number = 0

    @initialize(everyone_ready=sampled_from([True, True, True, True, False]))
    def start(self, everyone_ready: bool) -> None:
        # Twelve separate random clicks almost never line up before a timeout, so most runs
        # start past the ready check. Otherwise the later phases would barely get tested.
        if everyone_ready:
            for player in self.players[:12]:
                self._try(lobby.mark_ready, self.match.pk, player)

    def teardown(self) -> None:
        transaction.savepoint_rollback(self.savepoint)

    def _fresh(self) -> Match:
        return Match.objects.select_related("mode_version", "season", "community").get(
            pk=self.match.pk
        )

    def _try(self, call, *args) -> None:
        try:
            with transaction.atomic():
                call(*args)
        except DomainError:
            pass  # A refused action is a legitimate outcome. Invariants decide if it was right.

    @rule(index=integers(0, 14))
    def ready(self, index: int) -> None:
        self._try(lobby.mark_ready, self.match.pk, self.players[index])

    @rule(index=integers(0, 14))
    def keep_role(self, index: int) -> None:
        self._try(lobby.keep_captaincy, self.match.pk, self.players[index])

    @rule(captain=integers(0, 14), asked=integers(0, 14))
    def request_handoff(self, captain: int, asked: int) -> None:
        self._try(
            lobby.request_handoff, self.match.pk, self.players[captain], self.players[asked].pk
        )

    @rule(accept=sampled_from([True, False]))
    def answer_handoff(self, accept: bool) -> None:
        handoff = self._fresh().handoffs.filter(status="pending").first()
        if handoff is not None:
            self._try(lobby.respond_handoff, handoff.pk, handoff.to_player, accept)

    def _acting(self, match: Match, status: str) -> Player | None:
        if match.status != status:
            return None
        step = build_plan(mode_of(match))[match.step_index]
        first = (
            match.player_first_pick if status == Status.PLAYER_DRAFT else match.champion_first_pick
        )
        return captain_of(match, team_for_role(step.role, first)).player

    @rule(pick=integers(0, 11), double_click=sampled_from([False, True]))
    def pick_player(self, pick: int, double_click: bool) -> None:
        match = self._fresh()
        captain = self._acting(match, Status.PLAYER_DRAFT)
        if captain is None:
            return
        step = match.step_index
        undrafted = list(match.participants.filter(is_active=True, team=""))
        if not undrafted:
            return
        target = undrafted[pick % len(undrafted)].player_id
        self._try(draft.pick_player, match.pk, captain, step, target)
        if double_click:
            self._try(draft.pick_player, match.pk, captain, step, target)

    @rule(pick=integers(0, 43), double_click=sampled_from([False, True]))
    def pick_champion(self, pick: int, double_click: bool) -> None:
        match = self._fresh()
        captain = self._acting(match, Status.CHAMPION_DRAFT)
        if captain is None:
            return
        step = match.step_index
        champion = match.champion_pool[pick % len(match.champion_pool)]
        self._try(draft.choose_champion, match.pk, captain, step, champion)
        if double_click:
            self._try(draft.choose_champion, match.pk, captain, step, champion)

    @rule()
    def submit_code(self) -> None:
        match = self._fresh()
        if match.status == Status.LOBBY_CODE:
            self._try(draft.submit_lobby_code, match.pk, match.lobby_code_host, "ABC123")

    @rule(team=sampled_from(["A", "B"]), winner=sampled_from(["A", "B"]))
    def report(self, team: str, winner: str) -> None:
        match = self._fresh()
        if match.status not in {Status.IN_PROGRESS, Status.AWAITING_CONFIRMATION}:
            return
        self.image_number += 1
        self._try(
            results.submit_report,
            match.pk,
            captain_of(match, team).player,
            winner,
            image(self.image_number),
        )

    @rule(team=sampled_from(["A", "B"]))
    def confirm(self, team: str) -> None:
        match = self._fresh()
        if match.status == Status.AWAITING_CONFIRMATION:
            self._try(results.confirm, match.pk, captain_of(match, team).player)

    @rule(team=sampled_from(["A", "B"]))
    def dispute(self, team: str) -> None:
        match = self._fresh()
        if match.status == Status.AWAITING_CONFIRMATION:
            self._try(results.dispute, match.pk, captain_of(match, team).player, "No")

    @rule(outcome=sampled_from(["A", "B", "void"]))
    def staff_resolves(self, outcome: str) -> None:
        self._try(results.resolve_dispute, self.match.pk, outcome, None, "Staff")

    @rule(agree=sampled_from([True, True, False]))
    def play_through_current_phase(self, agree: bool) -> None:
        # Random clicks alone rarely finish a 10-pick draft inside one run, so this rule walks
        # a whole phase. The other rules still land at any point before and after it.
        match = self._fresh()
        if match.status == Status.CAPTAIN_PHASE:
            self._try(keep_captains, match)
        elif match.status == Status.PLAYER_DRAFT:
            self._try(run_player_draft, match)
        elif match.status == Status.CHAMPION_DRAFT:
            self._try(run_champion_draft, match)
        elif match.status == Status.LOBBY_CODE:
            self._try(draft.submit_lobby_code, match.pk, match.lobby_code_host, "ABC123")
        elif match.status == Status.IN_PROGRESS:
            self.image_number += 1
            captain = captain_of(match, "A").player
            self._try(results.submit_report, match.pk, captain, "A", image(self.image_number))
        elif match.status == Status.AWAITING_CONFIRMATION:
            captain = captain_of(match, "B").player
            if agree:
                self._try(results.confirm, match.pk, captain)
            else:
                self._try(results.dispute, match.pk, captain, "No")

    @rule(seconds=sampled_from([1, 31, 46, 181, 901, 11000]))
    def time_passes(self, seconds: int) -> None:
        self.clock.advance(seconds)
        deadlines.tick()

    @rule()
    def restart(self) -> None:
        deadlines.extend_past_due()

    @rule(roll=integers(0, 9))
    def staff_voids_live(self, roll: int) -> None:
        # Uncommon, or it would end most runs before they reach anything interesting.
        if roll == 0:
            self._try(lobby.staff_void, self.match.pk, None, "Called off")

    @rule(outcome=sampled_from(["A", "B", "void"]))
    def correct_finished(self, outcome: str) -> None:
        self._try(corrections.apply_outcome_correction, self.match.pk, outcome, None, "Fix")

    @rule(index=integers(12, 14))
    def late_joiner(self, index: int) -> None:
        self._try(queue.join, self.community_id, self.players[index])

    @invariant()
    def ledger_is_consistent(self) -> None:
        self.statuses_seen.add(self._fresh().status)
        assert integrity.check_all() == []

    @invariant()
    def draft_rows_match_step_index(self) -> None:
        match = self._fresh()
        if match.status in {Status.PLAYER_DRAFT, Status.CHAMPION_DRAFT}:
            assert DraftAction.objects.filter(match=match).count() == match.step_index

    @invariant()
    def live_match_always_has_a_way_forward(self) -> None:
        match = self._fresh()
        if match.status in LIVE:
            assert match.deadline_at is not None, f"{match.status} has no deadline: it could hang"

    @invariant()
    def captains_are_consistent(self) -> None:
        match = self._fresh()
        if match.status not in {Status.READY_CHECK, Status.VOIDED}:
            for team in ("A", "B"):
                assert match.participants.filter(is_captain=True, team=team).count() == 1

    @invariant()
    def teams_never_exceed_their_size(self) -> None:
        match = self._fresh()
        for team in ("A", "B"):
            assert match.participants.filter(team=team, removed_reason="").count() <= 6

    @invariant()
    def no_player_is_both_queued_and_live(self) -> None:
        for entry in QueueEntry.objects.select_related("player"):
            assert not entry.player.matches.filter(is_active=True).exists()

    @invariant()
    def no_champion_is_used_twice(self) -> None:
        match = self._fresh()
        used = list(
            match.draft_actions.exclude(target_champion=None).values_list(
                "target_champion_id", flat=True
            )
        )
        assert len(used) == len(set(used))


@pytest.mark.django_db
def test_match_machine(league, clock: Clock) -> None:
    from hypothesis.stateful import run_state_machine_as_test

    explore = bool(os.environ.get("EXPLORE"))
    MatchMachine.community_id = league.pk
    MatchMachine.clock = clock
    assert clock.now() >= timezone.now() - timedelta(seconds=5)
    run_state_machine_as_test(
        MatchMachine,
        settings=settings(
            # Fixed by default, so a failure means something changed rather than bad luck. Set
            # EXPLORE=1 for a longer hunt through sequences this run hasn't tried.
            derandomize=not explore,
            max_examples=150 if explore else 60,
            stateful_step_count=40,
            deadline=None,
            suppress_health_check=list(HealthCheck),
        ),
    )
    # Guards against the machine passing only because it never got anywhere interesting.
    assert {
        Status.CAPTAIN_PHASE,
        Status.PLAYER_DRAFT,
        Status.CHAMPION_DRAFT,
        Status.LOBBY_CODE,
        Status.IN_PROGRESS,
        Status.AWAITING_CONFIRMATION,
        Status.DISPUTED,
        Status.COMPLETED,
        Status.VOIDED,
    } <= MatchMachine.statuses_seen, MatchMachine.statuses_seen
