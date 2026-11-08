from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from league.services import champions


class Command(BaseCommand):
    help = "Compare champions with the Deadlock API. Prints the diff; --apply writes it."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        diff = champions.diff(champions.fetch_remote())
        if diff.is_empty:
            self.stdout.write("Roster is up to date.")
            return
        for hero in diff.new:
            self.stdout.write(f"New: {hero.name} (id {hero.id})")
        for champion, new_name in diff.renamed:
            self.stdout.write(f"Renamed: {champion.name} -> {new_name}")
        for champion in diff.missing_upstream:
            self.stdout.write(f"Not in the API, would be disabled: {champion.name}")
        if options["apply"]:
            champions.apply_diff(diff)
            self.stdout.write("Applied.")
        else:
            self.stdout.write("Re-run with --apply to write these changes.")
