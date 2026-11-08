from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the Discord bot."
    requires_system_checks = "__all__"

    def handle(self, *args: Any, **options: Any) -> None:
        from bot.client import run

        run()
