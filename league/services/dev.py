"""Fake players for local smoke tests. Real Discord IDs are far larger than this range."""

from __future__ import annotations

from league.models import Player

FAKE_ID_LIMIT = 1_000_000


def is_fake(discord_user_id: int) -> bool:
    return 0 < discord_user_id < FAKE_ID_LIMIT


def fake_players(count: int) -> list[Player]:
    players: list[Player] = []
    index = 1
    while len(players) < count:
        player, _created = Player.objects.get_or_create(
            discord_user_id=index, defaults={"display_name": f"Fake {index}"}
        )
        if not player.matches.filter(is_active=True).exists():
            players.append(player)
        index += 1
    return players
