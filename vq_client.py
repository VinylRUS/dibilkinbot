"""Village Quest — HTTP client for Transport A (panel → mod).

When the guild is configured with transport='A', this module makes direct
HTTP calls to the mod's REST API at http://<api_url>:8790/api/...

See rest_api.md §5–§9 in the mod repo for endpoint contract.

All requests are short (5s timeout) — if the MC server is lagging, the mod
returns 503 server_busy. Connection refused = server is in exarathon sleep
mode (not an error — caller should treat as "offline, retry later").

v3.1: covers GET endpoints + POST /api/links/claim + POST /api/quests +
      PATCH /api/quests/custom/{id} + DELETE /api/quests/custom/{id} +
      DELETE /api/links/{discord_id} + GET /api/events?after_id=
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

import vq_data

log = logging.getLogger("vq_client")

DEFAULT_TIMEOUT = 5.0  # seconds — mod has 5s budget per request


class VQError(Exception):
    """REST API error from the mod (4xx/5xx with {error, message} body)."""

    def __init__(self, status: int, code: str, message: str, body: Any = None):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body = body


class VQServerUnavailable(Exception):
    """Server unreachable / sleeping (exarathon hibernate)."""


class VQClient:
    """HTTP client bound to one guild's config (transport='A')."""

    def __init__(self, api_url: str, api_token: str):
        if not api_url:
            raise ValueError("api_url is required for transport A")
        self.base_url = api_url.rstrip("/")
        self.api_token = api_token

    @classmethod
    async def from_guild(cls, guild_id: int) -> Optional["VQClient"]:
        """Construct a client from per-guild config. Returns None if not configured."""
        api_url = await vq_data.get_api_url(guild_id)
        api_token = await vq_data.get_api_token(guild_id)
        if not api_url:
            return None
        return cls(api_url=api_url, api_token=api_token)

    def _headers(self) -> dict:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_token:
            h["Authorization"] = f"Bearer {self.api_token}"
        return h

    async def _request(self, method: str, path: str, *, query: str = "",
                       body: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                if body is not None:
                    r = await client.request(method, url, headers=self._headers(), json=body)
                else:
                    r = await client.request(method, url, headers=self._headers())
        except httpx.ConnectError as e:
            raise VQServerUnavailable(f"connection refused ({e})") from e
        except httpx.TimeoutException as e:
            raise VQServerUnavailable(f"timeout ({e})") from e
        except httpx.RequestError as e:
            raise VQServerUnavailable(f"network error: {e}") from e

        try:
            data = r.json()
        except Exception:
            data = None

        if r.status_code >= 400:
            code = (data or {}).get("error") or f"http_{r.status_code}"
            message = (data or {}).get("message") or r.text[:200]
            raise VQError(r.status_code, code, message, data)
        return data

    # === Endpoints (rest_api.md §5) — server & players ===

    async def health(self) -> dict:
        """GET /api/health — server status."""
        return await self._request("GET", "/api/health")

    async def list_players(self, *, all_players: bool = False) -> dict:
        """GET /api/players — list online players (or all if all_players=True)."""
        q = "all=true" if all_players else ""
        return await self._request("GET", "/api/players", query=q)

    async def get_player(self, uuid: str, *, locale: str = "") -> dict:
        """GET /api/players/{uuid} — player card."""
        q = f"locale={locale}" if locale else ""
        return await self._request("GET", f"/api/players/{uuid}", query=q)

    async def get_player_by_discord(self, discord_id: int) -> dict:
        """GET /api/players/by-discord/{discord_id} — player card by Discord ID.
        Raises VQError(404) if not linked."""
        return await self._request("GET", f"/api/players/by-discord/{int(discord_id)}")

    # === Endpoints (rest_api.md §6) — world & economy ===

    async def list_villages(self, *, limit: int = 200) -> dict:
        """GET /api/villages — flat list of player-owned villages."""
        return await self._request("GET", "/api/villages", query=f"limit={int(limit)}")

    async def list_caravans(self) -> dict:
        """GET /api/caravans — players with active trade routes."""
        return await self._request("GET", "/api/caravans")

    async def economy_summary(self) -> dict:
        """GET /api/economy/summary — server-wide economy snapshot."""
        return await self._request("GET", "/api/economy/summary")

    async def leaderboard(self, type_: str, *, limit: int = 10) -> dict:
        """GET /api/leaderboards/{type} — top N players.
        type: wealth | reputation | quests_total | route_successes | weekly_completed
        """
        return await self._request("GET", f"/api/leaderboards/{type_}",
                                    query=f"limit={int(limit)}")

    # === Endpoints (rest_api.md §7) — events ===

    async def list_events(self, after_id: int = 0, *, limit: int = 100) -> dict:
        """GET /api/events?after_id={id} — events with id > after_id."""
        return await self._request("GET", "/api/events",
                                    query=f"after_id={int(after_id)}&limit={int(limit)}")

    # === Endpoints (rest_api.md §8) — link flow ===

    async def claim_link(self, code: str, discord_id: int, discord_username: str) -> dict:
        """POST /api/links/claim — bind Discord ID to MC account via 6-digit code.
        Raises VQError(404) if code not found / expired."""
        return await self._request("POST", "/api/links/claim", body={
            "code": str(code),
            "discord_id": int(discord_id),
            "discord_username": str(discord_username),
        })

    async def get_link(self, discord_id: int) -> dict:
        """GET /api/links/{discord_id} — link only (no player card). 404 if not linked."""
        return await self._request("GET", f"/api/links/{int(discord_id)}")

    async def delete_link(self, discord_id: int) -> dict:
        """DELETE /api/links/{discord_id} — unlink."""
        return await self._request("DELETE", f"/api/links/{int(discord_id)}")

    # === Endpoints (rest_api.md §9) — quests ===

    async def list_quests(self) -> dict:
        """GET /api/quests — all quests (builtin + custom)."""
        return await self._request("GET", "/api/quests")

    async def get_quest(self, quest_id: str) -> dict:
        """GET /api/quests/{id} — single quest by id."""
        return await self._request("GET", f"/api/quests/{quest_id}")

    async def create_quest(self, quest_body: dict) -> dict:
        """POST /api/quests — create a custom quest. Body shape: rest_api.md §9 schema."""
        return await self._request("POST", "/api/quests", body=quest_body)

    async def update_quest(self, short_id: str, patch: dict) -> dict:
        """PATCH /api/quests/custom/{id} — partial update (e.g. {"enabled": false})."""
        return await self._request("PATCH", f"/api/quests/custom/{short_id}", body=patch)

    async def delete_quest(self, short_id: str) -> dict:
        """DELETE /api/quests/custom/{id} — delete custom quest."""
        return await self._request("DELETE", f"/api/quests/custom/{short_id}")
