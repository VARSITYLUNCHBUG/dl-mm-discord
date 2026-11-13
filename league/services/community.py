"""Community setup: the row for this Discord server, its first season and channel wiring."""

from __future__ import annotations

from django.db import transaction

from league.domain.errors import InvalidAction
from league.models import Community, DraftModeVersion, Season
from league.services.common import now

DEFAULT_MODE_SLUG = "bebot_2ban"


def get(guild_id: int) -> Community | None:
    return Community.objects.select_related("active_mode").filter(discord_guild_id=guild_id).first()


@transaction.atomic
def ensure(guild_id: int, name: str) -> Community:
    community = Community.objects.filter(discord_guild_id=guild_id).first()
    if community is None:
        mode = DraftModeVersion.objects.filter(slug=DEFAULT_MODE_SLUG).order_by("-version").first()
        if mode is None:
            raise InvalidAction("No draft modes are loaded. Run `manage.py load_presets` first.")
        community = Community.objects.create(
            discord_guild_id=guild_id, name=name[:100], active_mode=mode
        )
    if not Season.objects.filter(community=community, ended_at__isnull=True).exists():
        number = Season.objects.filter(community=community).count() + 1
        Season.objects.create(
            community=community, number=number, name=f"Season {number}", started_at=now()
        )
    return community


def save_wiring(community_id: int, **ids: int | None) -> None:
    allowed = {
        "queue_channel_id",
        "queue_message_id",
        "waiting_room_channel_id",
        "results_channel_id",
        "archive_channel_id",
        "match_category_id",
        "staff_role_id",
    }
    unknown = set(ids) - allowed
    if unknown:
        raise ValueError(f"Unknown wiring fields: {sorted(unknown)}")
    Community.objects.filter(pk=community_id).update(**ids)


@transaction.atomic
def start_season(community: Community, params: dict[str, object] | None = None) -> Season:
    """Close the open season and open the next. Ratings start fresh, with `params` over the
    OpenSkill defaults; they're fixed once the season rates a match."""
    from league.domain.openskill_v1 import DEFAULT_PARAMS

    unknown = set(params or {}) - set(DEFAULT_PARAMS)
    if unknown:
        raise InvalidAction(f"Unknown rating parameters: {', '.join(sorted(unknown))}.")
    moment = now()
    Season.objects.filter(community=community, ended_at__isnull=True).update(ended_at=moment)
    number = Season.objects.filter(community=community).count() + 1
    return Season.objects.create(
        community=community,
        number=number,
        name=f"Season {number}",
        rating_params=dict(params or {}),
        started_at=moment,
    )
