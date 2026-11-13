"""The champion roster: seeding from the preset and syncing from the community Deadlock API."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from django.conf import settings
from django.db import transaction
from django.utils.text import slugify

from league.models import Champion


def _slug(name: str, champion_id: int) -> str:
    return slugify(name.replace("&", "and")) or f"champion-{champion_id}"


def seed_from_file(path: Path) -> int:
    """Add champions missing from the table. Never changes rows admins may have edited."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    created = 0
    for entry in data["champions"]:
        _champion, was_created = Champion.objects.get_or_create(
            pk=entry["id"],
            defaults={
                "name": entry["name"],
                "slug": _slug(entry["name"], entry["id"]),
                "aliases": entry.get("aliases", []),
            },
        )
        created += 1 if was_created else 0
    return created


@dataclass(slots=True)
class EmojiLoad:
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _by_name() -> dict[str, Champion]:
    by_name: dict[str, Champion] = {}
    for champion in Champion.objects.all():
        by_name[champion.name.lower()] = champion
        for alias in champion.aliases:
            by_name.setdefault(alias.lower(), champion)
    return by_name


@transaction.atomic
def load_unicode_emojis(path: Path) -> EmojiLoad:
    """Set the Unicode fallback emojis from the file's `unicode` section. Safe to run again."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    by_name = _by_name()
    result = EmojiLoad()
    for name, emoji in (data.get("unicode") or {}).items():
        match = by_name.get(str(name).lower())
        if match is None:
            result.unknown.append(str(name))
        elif match.unicode_emoji == str(emoji):
            result.unchanged.append(match.name)
        else:
            match.unicode_emoji = str(emoji)
            match.save(update_fields=["unicode_emoji", "updated_at"])
            result.updated.append(match.name)
    return result


@transaction.atomic
def load_emojis(path: Path) -> EmojiLoad:
    """Set champions' custom emojis from a `{name: emoji_id}` file, by name or alias."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    by_name = _by_name()
    result = EmojiLoad()
    for name, emoji_id in (data.get("emojis") or {}).items():
        match = by_name.get(str(name).lower())
        if match is None:
            result.unknown.append(str(name))
        elif match.discord_emoji_id == int(emoji_id):
            result.unchanged.append(match.name)
        else:
            match.discord_emoji_id = int(emoji_id)
            match.save(update_fields=["discord_emoji_id", "updated_at"])
            result.updated.append(match.name)
    return result


@dataclass(frozen=True, slots=True)
class RemoteChampion:
    id: int
    name: str
    icon_url: str


@dataclass(slots=True)
class RosterDiff:
    new: list[RemoteChampion] = field(default_factory=list)
    renamed: list[tuple[Champion, str]] = field(default_factory=list)
    missing_upstream: list[Champion] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.new or self.renamed or self.missing_upstream)


def _icon(hero: dict[str, Any]) -> str:
    images = hero.get("images") or {}
    for key in ("icon_hero_card", "icon_image_small", "minimap_image"):
        if images.get(key):
            return str(images[key])
    return ""


def parse_remote(payload: list[dict[str, Any]]) -> list[RemoteChampion]:
    """Keep playable heroes. Deliberately ignores `in_development`, which is still set on some
    heroes that can be played, including Violet and Baba."""
    remote = []
    for hero in payload:
        if hero.get("disabled") or hero.get("player_selectable") is False:
            continue
        remote.append(
            RemoteChampion(id=int(hero["id"]), name=str(hero["name"]), icon_url=_icon(hero))
        )
    return remote


def fetch_remote() -> list[RemoteChampion]:
    """Network call. Run it outside any database transaction or the bot's DB thread."""
    url = f"{settings.DEADLOCK_API_BASE}/v1/assets/heroes"
    response = httpx.get(url, params={"only_active": "true"}, timeout=15)
    response.raise_for_status()
    return parse_remote(response.json())


def diff(remote: list[RemoteChampion]) -> RosterDiff:
    current = {champion.pk: champion for champion in Champion.objects.all()}
    result = RosterDiff()
    remote_ids = set()
    for hero in remote:
        remote_ids.add(hero.id)
        existing = current.get(hero.id)
        if existing is None:
            result.new.append(hero)
        elif existing.name != hero.name:
            result.renamed.append((existing, hero.name))
    result.missing_upstream = [
        champion for pk, champion in current.items() if pk not in remote_ids and champion.is_enabled
    ]
    return result


@transaction.atomic
def apply_diff(roster_diff: RosterDiff) -> None:
    """Apply after an admin has confirmed. New champions start enabled; missing ones are disabled
    rather than deleted, so past matches still resolve."""
    for hero in roster_diff.new:
        Champion.objects.create(
            pk=hero.id,
            name=hero.name,
            slug=_slug(hero.name, hero.id),
            icon_url=hero.icon_url,
        )
    for champion, new_name in roster_diff.renamed:
        champion.name = new_name
        champion.save(update_fields=["name", "updated_at"])
    for champion in roster_diff.missing_upstream:
        champion.is_enabled = False
        champion.save(update_fields=["is_enabled", "updated_at"])
