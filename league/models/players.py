from __future__ import annotations

from django.db import models


class Player(models.Model):
    """A Discord identity. Elo and history are derived: see PlayerRating and RatingEvent."""

    discord_user_id = models.BigIntegerField(unique=True)
    display_name = models.CharField(max_length=100)
    steam_account_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "players"

    def __str__(self) -> str:
        return self.display_name
