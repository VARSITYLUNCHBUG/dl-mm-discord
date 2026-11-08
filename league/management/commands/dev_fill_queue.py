from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from league.models import Community
from league.services import dev, queue


class Command(BaseCommand):
    help = "Local smoke tests only: add fake players to the queue. Refused unless DEBUG is on."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--count", type=int, default=11)

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG or not settings.DEV_FAKE_PLAYERS:
            raise CommandError("Set DEBUG=true and DEV_FAKE_PLAYERS=true to use this.")
        community = Community.objects.first()
        if community is None:
            raise CommandError("No community yet: run /setup in Discord first.")
        added = 0
        for player in dev.fake_players(options["count"]):
            try:
                queue.join(community.pk, player)
                added += 1
            except Exception as exc:
                self.stderr.write(f"{player}: {exc}")
        self.stdout.write(f"Added {added} fake players.")
