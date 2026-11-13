"""Reading a Steam account from whatever a player pastes. Pure: no network.

Deadlock and deadlock-api identify players by the 32-bit account ID, which is also the friend
code Steam shows under Add a Friend. A SteamID64 is that number plus a fixed base.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STEAM64_BASE = 76561197960265728
MAX_ACCOUNT_ID = 2**32 - 1


@dataclass(frozen=True, slots=True)
class SteamRef:
    account_id: int | None = None  # Known straight away.
    vanity: str = ""  # A custom URL name that still has to be resolved.


def account_from_steam64(steam64: int) -> int | None:
    account = steam64 - STEAM64_BASE
    return account if 0 < account <= MAX_ACCOUNT_ID else None


def steam64_from_account(account_id: int) -> int:
    return account_id + STEAM64_BASE


def parse(raw: str) -> SteamRef | None:
    """A profile URL, SteamID64, [U:1:n], STEAM_0:y:z, friend code or account ID. None if it
    isn't any of those."""
    text = raw.strip().strip("<>").rstrip("/")
    if not text:
        return None
    vanity = re.search(r"steamcommunity\.com/id/([A-Za-z0-9_-]{2,32})$", text)
    if vanity:
        return SteamRef(vanity=vanity[1])
    profile = re.search(r"steamcommunity\.com/profiles/(\d{17})$", text)
    if profile:
        account = account_from_steam64(int(profile[1]))
        return SteamRef(account_id=account) if account else None
    steam3 = re.fullmatch(r"\[?U:1:(\d{1,10})\]?", text)
    if steam3:
        account = int(steam3[1])
        return SteamRef(account_id=account) if 0 < account <= MAX_ACCOUNT_ID else None
    steam2 = re.fullmatch(r"STEAM_[0-5]:([01]):(\d{1,10})", text, re.IGNORECASE)
    if steam2:
        account = int(steam2[2]) * 2 + int(steam2[1])
        return SteamRef(account_id=account) if 0 < account <= MAX_ACCOUNT_ID else None
    if re.fullmatch(r"\d{17}", text):
        account = account_from_steam64(int(text))
        return SteamRef(account_id=account) if account else None
    if re.fullmatch(r"\d{1,10}", text):
        account = int(text)
        return SteamRef(account_id=account) if 0 < account <= MAX_ACCOUNT_ID else None
    return None
