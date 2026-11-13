from __future__ import annotations

from django.db import models
from django.db.models import Q


class Player(models.Model):
    """A Discord identity. Ratings and history are derived: see PlayerRating and RatingEvent."""

    discord_user_id = models.BigIntegerField(unique=True)
    display_name = models.CharField(max_length=100)
    # Optional. The 32-bit Steam account ID, as deadlock-api and match metadata use it.
    steam_account_id = models.BigIntegerField(null=True, blank=True)
    steam_name = models.CharField(max_length=100, blank=True)
    steam_linked_at = models.DateTimeField(null=True, blank=True)
    # Set once that account turned up, on the player's own team, in one of their matches.
    steam_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "players"
        constraints = [
            models.UniqueConstraint(
                fields=("steam_account_id",),
                condition=Q(steam_account_id__isnull=False),
                name="uniq_player_steam_account",
            ),
        ]

    def __str__(self) -> str:
        return self.display_name
