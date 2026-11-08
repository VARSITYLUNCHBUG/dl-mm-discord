from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from league.services import integrity


class Command(BaseCommand):
    help = "Verify the rating ledger: zero-sum runs, projections and replay all agree."

    def handle(self, *args: Any, **options: Any) -> None:
        problems = integrity.check_all()
        if problems:
            for problem in problems:
                self.stderr.write(problem)
            raise CommandError(f"{len(problems)} ledger problem(s) found.")
        self.stdout.write("Ledger OK.")
