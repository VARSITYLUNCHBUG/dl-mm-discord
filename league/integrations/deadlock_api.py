"""A small client for deadlock-api.com, the community API for Deadlock match data.

Every call is a blocking network request: run it off the event loop and outside any database
transaction (`asyncio.to_thread` from the bot). Nothing here touches the database.

deadlock-api is community-run and unofficial, so callers treat every failure as ordinary: a
lobby falls back to a captain entering a code, and a result check is retried or skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings

TIMEOUT = httpx.Timeout(10.0, read=60.0)  # Lobby creation waits for Deadlock to make the party.
USER_AGENT = "dl-mm-discord (Deadlock inhouse bot)"

GAME_MODES = ("normal", "street_brawl")
SERVER_REGIONS = (
    "europe", "eu_amsterdam", "eu_poland", "eu_stockholm", "eu_helsinki", "eu_falkenstein",
    "eu_spain", "eu_east", "eu_london", "south_africa", "us_west", "us_east", "us_north_central",
    "us_south_central", "us_south_east", "us_south_west", "australia", "singapore", "japan",
    "hong_kong", "mp_hong_kong", "seoul", "chile", "peru", "argentina", "south_america",
)  # fmt: skip


class ApiError(Exception):
    """A request that didn't succeed. `status` is the HTTP status, or None for a network error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


def _headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if settings.DEADLOCK_API_KEY:
        headers["X-API-KEY"] = settings.DEADLOCK_API_KEY
    return headers


def _client() -> httpx.Client:
    return httpx.Client(base_url=settings.DEADLOCK_API_BASE, headers=_headers(), timeout=TIMEOUT)


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    try:
        with _client() as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise ApiError(f"deadlock-api unreachable: {type(exc).__name__}") from exc
    if response.status_code >= 400 and response.status_code != 404:
        detail = response.text[:200].strip()
        raise ApiError(f"deadlock-api {response.status_code}: {detail}", response.status_code)
    return response


# --- Matches ---------------------------------------------------------------------------------


def match_metadata(match_id: int) -> dict[str, Any] | None:
    """A finished match's metadata, or None when deadlock-api doesn't have it (yet)."""
    response = _request("GET", f"/v1/matches/{match_id}/metadata")
    if response.status_code == 404:
        return None
    data = response.json()
    return data if isinstance(data, dict) else None


# --- Custom lobbies --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreatedLobby:
    party_id: str
    party_code: str


def create_custom_match(region: str = "", game_mode: str = "normal") -> CreatedLobby:
    """Have deadlock-api's bot host a custom lobby. It readies itself, spectates, and leaves 15
    minutes after creating it, whatever the lobby is doing."""
    body: dict[str, Any] = {"game_mode": game_mode, "is_publicly_visible": False}
    if region:
        body["server_region"] = region
    response = _request("POST", "/v1/matches/custom/create", json=body)
    if response.status_code == 404:
        raise ApiError("deadlock-api has no custom match endpoint", 404)
    data = response.json()
    party_id, code = str(data.get("party_id") or ""), str(data.get("party_code") or "")
    if not party_id or not code:
        raise ApiError("deadlock-api returned no party code")
    return CreatedLobby(party_id=party_id, party_code=code)


def start_custom_match(party_id: str) -> None:
    response = _request("POST", f"/v1/matches/custom/{party_id}/start")
    if response.status_code == 404:
        raise ApiError("That lobby no longer exists", 404)


def custom_match_id(party_id: str) -> int | None:
    """The Deadlock match ID of a started custom lobby, or None if it isn't known yet."""
    response = _request("GET", f"/v1/matches/custom/{party_id}/match-id")
    if response.status_code == 404:
        return None
    match_id = response.json().get("match_id")
    return int(match_id) if isinstance(match_id, int) and match_id > 0 else None


# --- Steam -----------------------------------------------------------------------------------


def steam_names(account_ids: list[int]) -> dict[int, str]:
    """Steam display names by 32-bit account ID. Unknown accounts are left out."""
    if not account_ids:
        return {}
    response = _request(
        "GET", "/v1/players/steam", params={"account_ids": ",".join(map(str, account_ids))}
    )
    if response.status_code == 404:
        return {}
    rows = response.json()
    if not isinstance(rows, list):
        return {}
    return {
        int(row["account_id"]): str(row.get("personaname") or "")
        for row in rows
        if isinstance(row, dict) and "account_id" in row
    }


def resolve_vanity(name: str) -> int | None:
    """A custom profile URL name to a SteamID64, through Steam's own API. Needs
    STEAM_WEB_API_KEY; without it, or for an unknown name, None."""
    if not settings.STEAM_WEB_API_KEY:
        return None
    try:
        response = httpx.get(
            "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
            params={"key": settings.STEAM_WEB_API_KEY, "vanityurl": name},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ApiError(f"Steam unreachable: {type(exc).__name__}") from exc
    body = response.json().get("response", {})
    if body.get("success") != 1:
        return None
    return int(body["steamid"])
