from __future__ import annotations

import copy

from django.db import models

DEFAULT_PENALTY_POLICY = {
    # Cooldown minutes by rung. A clean record uses the first rung; each strike a player already
    # carries, from any kind of offence, moves them one rung up. Starting values are judgement.
    "ladders": {
        "missed_ready": [5, 15, 60, 180, 720],
        "dodge": [60, 360, 1440, 4320, 10080],
        "leaver": [120, 720, 2880, 10080],
        "false_report": [1440, 4320, 10080],
    },
    # Strikes each kind of offence adds. A staff "manual" cooldown adds none.
    "strikes": {"missed_ready": 1, "dodge": 2, "leaver": 3, "false_report": 3, "manual": 0},
    # Clean completed matches that work off one strike.
    "games_per_strike": 3,
    # One strike also comes off for each this many days without an offence.
    "decay_days": 14,
    "max_strikes": 10,
}

OCR_MODES = (
    ("off", "Off"),
    ("shadow", "Shadow: read and store stats only"),
    ("on", "On: stats, MVP and key players, and cross-checks"),
)


def default_penalty_policy() -> dict[str, object]:
    return copy.deepcopy(DEFAULT_PENALTY_POLICY)


def effective_penalty_policy(stored: dict[str, object] | None) -> dict[str, object]:
    """The stored policy over the defaults, one level deep, so a server that saved an older
    policy still gets keys added since. Values a server has set are never replaced."""
    merged = default_penalty_policy()
    for key, value in (stored or {}).items():
        default = merged.get(key)
        if isinstance(default, dict) and isinstance(value, dict):
            merged[key] = {**default, **value}
        else:
            merged[key] = value
    return merged


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
    # Reading report screenshots. Needs the `ocr` dependency group installed.
    score_ocr = models.CharField(max_length=8, choices=OCR_MODES, default="off")
    # deadlock-api: create the custom lobby for each match, and check results by match ID.
    auto_lobby = models.BooleanField(default=False)
    lobby_region = models.CharField(max_length=24, blank=True)  # Empty: the API's default.
    api_results = models.BooleanField(default=True)

    class Meta:
        db_table = "communities"
        verbose_name_plural = "communities"

    def __str__(self) -> str:
        return self.name
