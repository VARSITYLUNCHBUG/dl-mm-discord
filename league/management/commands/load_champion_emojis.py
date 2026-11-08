from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from league.services import champions


class Command(BaseCommand):
    help = "Set champions' custom emojis from presets/champion_emojis.yaml. Safe to run again."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--file", default=str(settings.BASE_DIR / "presets/champion_emojis.yaml")
        )

    def handle(self, *args: Any, **options: Any) -> None:
        result = champions.load_emojis(Path(options["file"]))
        self.stdout.write(
            f"Emojis set: {len(result.updated)}, already set: {len(result.unchanged)}"
        )
        for name in result.unknown:
            self.stdout.write(f"No champion called {name!r}: skipped")
