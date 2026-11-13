"""Optional Steam accounts: one per Discord player, and one player per account.

A link is what the player says. It becomes verified when deadlock-api shows that account on the
player's own team in one of their matches, which nobody can fake from Discord. Verified links let
result checks and awards work by account instead of by scoreboard name.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction

from league.domain.errors import InvalidAction
from league.models import Player
from league.services.common import now


@transaction.atomic
def link(player: Player, account_id: int, steam_name: str = "") -> Player:
    taken = (
        Player.objects.select_for_update()
        .filter(steam_account_id=account_id)
        .exclude(pk=player.pk)
        .first()
    )
    if taken is not None:
        raise InvalidAction(
            "That Steam account is already linked to someone else. Ask staff if it's yours."
        )
    row = Player.objects.select_for_update().get(pk=player.pk)
    if row.steam_account_id != account_id:
        row.steam_verified_at = None
    row.steam_account_id = account_id
    row.steam_name = steam_name[:100]
    row.steam_linked_at = now()
    row.save(
        update_fields=[
            "steam_account_id",
            "steam_name",
            "steam_linked_at",
            "steam_verified_at",
            "updated_at",
        ]
    )
    return row


@transaction.atomic
def unlink(player: Player) -> bool:
    updated = Player.objects.filter(pk=player.pk, steam_account_id__isnull=False).update(
        steam_account_id=None, steam_name="", steam_linked_at=None, steam_verified_at=None
    )
    return bool(updated)


def mark_verified(player_ids: Iterable[int]) -> int:
    """Linked accounts that deadlock-api showed on the right team."""
    ids = list(player_ids)
    if not ids:
        return 0
    return Player.objects.filter(pk__in=ids, steam_verified_at__isnull=True).update(
        steam_verified_at=now()
    )


def describe(player: Player) -> str:
    if player.steam_account_id is None:
        return "No Steam account linked. Use /link to add one (optional)."
    name = f"**{player.steam_name}**" if player.steam_name else "an account"
    status = "verified in a match" if player.steam_verified_at else "not yet seen in a match"
    return f"Steam: {name} (account {player.steam_account_id}), {status}."
