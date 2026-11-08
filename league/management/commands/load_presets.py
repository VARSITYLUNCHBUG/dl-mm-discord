from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from league.services import champions, modes

# Data files in presets/ that aren't draft modes.
NOT_MODES = frozenset({"champions.yaml", "champion_emojis.yaml"})


class Command(BaseCommand):
    help = "Seed missing champions and publish preset draft modes. Safe to run repeatedly."

    def handle(self, *args: Any, **options: Any) -> None:
        presets = settings.BASE_DIR / "presets"
        added = champions.seed_from_file(presets / "champions.yaml")
        self.stdout.write(f"Champions added: {added}")
        for path in sorted(presets.glob("*.yaml")):
            if path.name in NOT_MODES:
                continue
            version = modes.publish(modes.load_yaml(path))
            self.stdout.write(f"Mode ready: {version}")
