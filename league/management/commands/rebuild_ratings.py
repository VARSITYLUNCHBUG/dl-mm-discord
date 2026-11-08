from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from league.models import Season
from league.services.corrections import rebuild_projection


class Command(BaseCommand):
    help = "Recreate player_ratings from the ledger. Never writes to the ledger itself."

    def handle(self, *args: Any, **options: Any) -> None:
        for season in Season.objects.all():
            with transaction.atomic():
                touched = rebuild_projection(season)
            self.stdout.write(f"{season}: {touched} rows rebuilt.")
