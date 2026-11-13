from __future__ import annotations

from django.apps import AppConfig
from django.db.models.signals import post_migrate


class LeagueConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "league"

    def ready(self) -> None:
        from league.db_guards import on_post_migrate

        # History tables are append-only; the triggers are installed after every migrate.
        post_migrate.connect(on_post_migrate, sender=self, dispatch_uid="league_append_only")
