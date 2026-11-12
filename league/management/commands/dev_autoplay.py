"""Local smoke tests only: play a whole match through the real services, slowly enough to watch.

The running bot does all the Discord work from the outbox, exactly as in a real match. This command
only stands in for the people clicking, so it needs that bot running alongside it. It does not
exercise button handling, which is covered by tests and real clicks.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from league.domain.draft_plan import build_plan, team_for_role
from league.domain.engine import Status
from league.domain.errors import DomainError
from league.domain.mode_schema import DraftMode, parse_mode
from league.models import Community, DraftModeVersion, Match, Player, ResultSubmission
from league.services import dev, draft, integrity, lobby, modes, queue, results
from league.services.common import captain_of, mode_of
from league.services.results import ImageRef


class Command(BaseCommand):
    help = "Local smoke tests only: play a full match. Refused unless DEBUG is on."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--mode", default="bebot_2ban", help="Draft mode slug")
        parser.add_argument("--lead-in", type=float, default=0, help="Seconds before starting")
        parser.add_argument("--pause", type=float, default=20, help="Seconds at each phase")
        parser.add_argument("--step", type=float, default=2, help="Seconds between draft steps")
        parser.add_argument("--winner", choices=("A", "B"), default="B")

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG or not settings.DEV_FAKE_PLAYERS:
            raise CommandError("Set DEBUG=true and DEV_FAKE_PLAYERS=true to use this.")
        self.pause_seconds = options["pause"]
        self.step_seconds = options["step"]
        community = Community.objects.first()
        if community is None:
            raise CommandError("No community yet: run /setup in Discord first.")
        image = self._reusable_screenshot()

        version = DraftModeVersion.objects.filter(slug=options["mode"]).order_by("-version").first()
        if version is None:
            raise CommandError(f"No mode called {options['mode']}.")
        modes.activate(community.pk, version.pk)
        self._say(f"Mode: {version}")

        time.sleep(options["lead_in"])
        match = self._fill_queue(community)
        self._say(f"Match #{match.pk} popped")
        self._pause("Ready check")

        for participant in self._fresh(match).participants.filter(is_active=True):
            self._act(lobby.mark_ready, match.pk, participant.player)
        self._captain_vote(match)
        self._player_draft(match)
        self._side_select(match)
        self._champion_draft(match)
        self._lobby_code(match)
        self._report(match, options["winner"], image)

        problems = integrity.check_all()
        self.stdout.write(self.style.SUCCESS("Ledger OK") if not problems else str(problems))

    def _fresh(self, match: Match) -> Match:
        return Match.objects.select_related("mode_version", "season", "community").get(pk=match.pk)

    def _say(self, text: str) -> None:
        self.stdout.write(text)
        self.stdout.flush()

    def _pause(self, label: str) -> None:
        self._say(f"-- {label}: holding {self.pause_seconds:.0f}s for screenshots")
        time.sleep(self.pause_seconds)

    def _act(self, call: Callable[..., Any], *args: Any) -> Any:
        try:
            return call(*args)
        except DomainError as exc:
            self._say(f"   refused: {exc.message}")
            return None

    def _reusable_screenshot(self) -> ImageRef:
        """Reuse an archived scoreboard, so nothing has to be uploaded by hand."""
        shot = (
            ResultSubmission.objects.filter(archive_message_id__isnull=False)
            .order_by("-id")
            .first()
        )
        if shot is None or shot.archive_channel_id is None or shot.attachment_id is None:
            raise CommandError("No archived screenshot yet: report one match by hand first.")
        return ImageRef(
            sha256=shot.image_sha256,
            archive_channel_id=shot.archive_channel_id,
            archive_message_id=shot.archive_message_id or 0,
            attachment_id=shot.attachment_id,
        )

    def _fill_queue(self, community: Community) -> Match:
        """Everyone real who has ever played goes in first, so they see the lobby too."""
        size = mode_of_active(community).lobby_size
        real = list(Player.objects.filter(discord_user_id__gte=dev.FAKE_ID_LIMIT)[:size])
        before = Match.objects.order_by("-id").values_list("pk", flat=True).first() or 0
        for player in [*real, *dev.fake_players(size - len(real))]:
            self._act(queue.join, community.pk, player)
        match = Match.objects.filter(pk__gt=before).order_by("-id").first()
        if match is None:
            raise CommandError("The queue didn't pop: is someone already queued or penalised?")
        return match

    def _captain_vote(self, match: Match) -> None:
        """Everyone votes for two random players. Real players' votes are cast for them too."""
        self._pause("Captain vote")
        voters = list(match.participants.filter(is_active=True).select_related("player"))
        ids = [row.player_id for row in voters]
        for row in voters:
            if self._fresh(match).status != Status.CAPTAIN_PHASE:
                break
            self._act(lobby.cast_captain_votes, match.pk, row.player, random.sample(ids, 2))
            time.sleep(self.step_seconds)

    def _side_select(self, match: Match) -> None:
        match = self._fresh(match)
        self._pause("Teams set: claim a side")
        team = random.choice(["A", "B"])
        side = random.choice(["hk", "am"])
        self._act(draft.claim_side, match.pk, captain_of(match, team).player, side)

    def _player_draft(self, match: Match) -> None:
        self._pause("Player draft started")
        match = self._fresh(match)
        while match.status == Status.PLAYER_DRAFT:
            step = build_plan(mode_of(match))[match.step_index]
            captain = captain_of(match, team_for_role(step.role, match.player_first_pick)).player
            options = list(match.participants.filter(is_active=True, team=""))
            self._act(
                draft.pick_player,
                match.pk,
                captain,
                match.step_index,
                random.choice(options).player_id,
            )
            time.sleep(self.step_seconds)
            match = self._fresh(match)

    def _champion_draft(self, match: Match) -> None:
        self._pause("Teams set: check team voice")
        match = self._fresh(match)
        while match.status == Status.CHAMPION_DRAFT:
            step = build_plan(mode_of(match))[match.step_index]
            captain = captain_of(match, team_for_role(step.role, match.champion_first_pick)).player
            if random.random() < 0.2:  # Now and then press Random, so the 🎲 shows.
                self._act(draft.choose_random_champion, match.pk, captain, match.step_index)
            else:
                champion = random.choice(draft.available_champions(match))
                self._act(draft.choose_champion, match.pk, captain, match.step_index, champion)
            time.sleep(self.step_seconds)
            match = self._fresh(match)

    def _lobby_code(self, match: Match) -> None:
        self._pause("Lobby code prompt")
        match = self._fresh(match)
        if match.lobby_code_host is None:
            raise CommandError("No lobby host was chosen.")
        self._act(draft.submit_lobby_code, match.pk, match.lobby_code_host, "AUTO-TEST")

    def _report(self, match: Match, winner: str, image: ImageRef) -> None:
        self._pause("Game on: lobby code posted")
        match = self._fresh(match)
        first = captain_of(match, "A").player
        second = captain_of(match, "B").player
        self._act(results.submit_report, match.pk, first, winner, image)
        self._pause("Waiting for the other captain")
        self._act(results.confirm, match.pk, second)
        match = self._fresh(match)
        self._say(f"Match #{match.pk}: {match.status}, winner {match.winner}")


def mode_of_active(community: Community) -> DraftMode:
    community.refresh_from_db()
    return parse_mode(community.active_mode.body)
