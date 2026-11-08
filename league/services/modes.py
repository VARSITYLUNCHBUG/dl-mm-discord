"""Publishing draft modes. Versions are immutable; a change always makes a new row."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from django.db import transaction
from django.db.models import Max

from league.domain.engine import dry_run
from league.domain.mode_schema import parse_mode
from league.models import Champion, Community, DraftModeVersion


def content_hash(body: object) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _enabled_pool() -> list[int]:
    return list(Champion.objects.filter(is_enabled=True).values_list("pk", flat=True))


def validate_body(body: object) -> None:
    """Raise ModeValidationError with readable paths if this body is not usable right now."""
    dry_run(parse_mode(body), _enabled_pool())


@transaction.atomic
def publish(body: dict[str, object], created_by: Any = None) -> DraftModeVersion:
    """Validate and store a mode. Publishing identical content returns the existing version."""
    mode = parse_mode(body)
    dry_run(mode, _enabled_pool())
    digest = content_hash(body)
    latest = DraftModeVersion.objects.filter(slug=mode.slug).order_by("-version").first()
    if latest is not None and latest.content_hash == digest:
        return latest
    highest = DraftModeVersion.objects.filter(slug=mode.slug).aggregate(v=Max("version"))["v"] or 0
    return DraftModeVersion.objects.create(
        slug=mode.slug,
        version=highest + 1,
        name=mode.name,
        body=body,
        content_hash=digest,
        created_by=created_by,
    )


def load_yaml(path: Path) -> dict[str, object]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@transaction.atomic
def activate(community_id: int, version_id: int) -> DraftModeVersion:
    """New lobbies use this version. Live matches keep the version they pinned at creation."""
    version = DraftModeVersion.objects.get(pk=version_id)
    validate_body(version.body)
    community = Community.objects.select_for_update().get(pk=community_id)
    community.active_mode = version
    community.save(update_fields=["active_mode"])
    return version


def latest_versions() -> list[DraftModeVersion]:
    latest: dict[str, DraftModeVersion] = {}
    for version in DraftModeVersion.objects.order_by("slug", "-version"):
        latest.setdefault(version.slug, version)
    return list(latest.values())
