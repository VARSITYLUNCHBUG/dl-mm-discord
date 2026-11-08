from __future__ import annotations

from django.conf import settings
from django.db import models


class DraftModeVersion(models.Model):
    """An immutable draft mode. Editing one writes a new version, and each match pins its own."""

    slug = models.SlugField(max_length=64)
    version = models.PositiveIntegerField()
    name = models.CharField(max_length=120)
    body = models.JSONField()
    content_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "draft_mode_versions"
        ordering = ("slug", "-version")
        constraints = [
            models.UniqueConstraint(fields=("slug", "version"), name="uniq_mode_slug_version"),
        ]

    def __str__(self) -> str:
        return f"{self.slug} v{self.version}"
