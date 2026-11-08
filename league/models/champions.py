from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models


class Champion(models.Model):
    """A playable hero. `id` is the Deadlock API hero ID. Disable rows; never delete or reuse."""

    id = models.PositiveIntegerField(primary_key=True)
    name = models.CharField(max_length=64)
    slug = models.SlugField(max_length=64, unique=True)
    aliases = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    icon_url = models.URLField(max_length=500, blank=True)
    discord_emoji_id = models.BigIntegerField(null=True, blank=True)
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "champions"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name
