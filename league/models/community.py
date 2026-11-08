from __future__ import annotations

from django.db import models

DEFAULT_PENALTY_POLICY = {
    # Minutes for the 1st, 2nd, 3rd+ offence of each kind. Starting values are judgement.
    "ladders": {
        "missed_ready": [5, 15, 60],
        "dodge": [60, 360, 1440],
        "leaver": [120, 720, 2880],
        "false_report": [1440, 4320, 10080],
    },
    # An offence stops counting towards the ladder after this many days.
    "decay_days": 14,
}


def default_penalty_policy() -> dict[str, object]:
    return dict(DEFAULT_PENALTY_POLICY)


class Community(models.Model):
    """One Discord server and its wiring. Rules that govern a match live in the mode version."""

    discord_guild_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=100)
    active_mode = models.ForeignKey(
        "league.DraftModeVersion", on_delete=models.PROTECT, related_name="+"
    )
    staff_role_id = models.BigIntegerField(null=True, blank=True)
    queue_channel_id = models.BigIntegerField(null=True, blank=True)
    queue_message_id = models.BigIntegerField(null=True, blank=True)
    waiting_room_channel_id = models.BigIntegerField(null=True, blank=True)
    results_channel_id = models.BigIntegerField(null=True, blank=True)
    archive_channel_id = models.BigIntegerField(null=True, blank=True)
    match_category_id = models.BigIntegerField(null=True, blank=True)
    max_concurrent_matches = models.PositiveSmallIntegerField(default=12)
    penalty_policy = models.JSONField(default=default_penalty_policy)

    class Meta:
        db_table = "communities"
        verbose_name_plural = "communities"

    def __str__(self) -> str:
        return self.name
