from __future__ import annotations

import sys
import time
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

MAX_AGE_SECONDS = 60


class Command(BaseCommand):
    help = "Exit non-zero if the bot's task loops have stopped updating the heartbeat file."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            age = time.time() - settings.BOT_HEARTBEAT_FILE.stat().st_mtime
        except OSError:
            self.stderr.write("No heartbeat file.")
            sys.exit(1)
        if age > MAX_AGE_SECONDS:
            self.stderr.write(f"Heartbeat is {age:.0f}s old.")
            sys.exit(1)
