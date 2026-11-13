from __future__ import annotations

from django.db import models


class ChannelPing(models.Model):
    """A named ping members can send in one channel (/ping), with a shared cooldown."""

    community = models.ForeignKey("league.Community", on_delete=models.PROTECT, related_name="+")
    channel_id = models.BigIntegerField()
    name = models.CharField(max_length=50)
    mention = models.CharField(max_length=500)
    timer_minutes = models.PositiveIntegerField()
    # Only members holding this role can send it. Empty means everyone.
    required_role_id = models.BigIntegerField(null=True, blank=True)
    last_ping_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "channel_pings"
        ordering = ("channel_id", "name")
        constraints = [
            models.UniqueConstraint(fields=("channel_id", "name"), name="uniq_channel_ping_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} in {self.channel_id}"
