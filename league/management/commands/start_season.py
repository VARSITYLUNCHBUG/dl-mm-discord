from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from league.domain.errors import DomainError
from league.models import Community
from league.services.community import start_season


class Command(BaseCommand):
    help = "Close the open season and start the next one, with fresh ratings."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--guild", type=int, help="Discord guild ID. Optional with one server.")
        parser.add_argument(
            "--params", help="JSON of OpenSkill parameters over the defaults, such as beta"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        communities = Community.objects.all()
        if options["guild"]:
            communities = communities.filter(discord_guild_id=options["guild"])
        if communities.count() != 1:
            raise CommandError("Pass --guild: there isn't exactly one server to choose.")
        params = json.loads(options["params"]) if options["params"] else None
        try:
            season = start_season(communities.get(), params)
        except DomainError as exc:
            raise CommandError(exc.message) from exc
        self.stdout.write(f"Started {season}.")
