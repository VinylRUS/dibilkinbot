"""Village Quest — abstraction layer over transport A/B.

This module provides a unified API surface to the rest of the bot:
callers don't need to know if the guild is configured for transport A
(panel → mod HTTP client) or transport B (mod → panel gateway with
async command queue).

For transport A: calls are synchronous HTTP requests via vq_client.
For transport B: calls are enqueued as virtual HTTP commands; we poll
the local DB for results. The mod polls every poll_seconds (default 2s)
and sends back results via POST /vq/results. We wait up to
RESULT_TIMEOUT_SECONDS for the result.

v3.1
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import vq_client
import vq_data
from vq_client import VQClient, VQError, VQServerUnavailable

log = logging.getLogger("vq_api")

# How long to wait for transport B result before timing out.
# Mod polls every poll_seconds (default 2s), so typical latency is 2-4s.
# We give 15s headroom for lag.
RESULT_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.2


# === Helpers ===

async def get_client_for_guild(guild_id: int) -> VQClient | None:
    """For transport A: returns VQClient. For transport B: returns None."""
    transport = await vq_data.get_transport(guild_id)
    if transport != "A":
        return None
    return await VQClient.from_guild(guild_id)


async def _wait_for_command_result(guild_id: int, command_id: int) -> dict:
    """Poll DB for command result. Returns {status, result} or {error: timeout}."""
    deadline = asyncio.get_event_loop().time() + RESULT_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        cmd = await vq_data.get_command(guild_id, command_id)
        if cmd and cmd.get("status") is not None:
            return {
                "status": cmd["status"],
                "result": cmd.get("result"),
            }
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return {"error": "timeout", "message": f"mod did not respond within {RESULT_TIMEOUT_SECONDS}s"}


async def _transport_b_call(guild_id: int, method: str, path: str,
                             query: str = "", body: Any = None) -> dict:
    """Enqueue a virtual HTTP command for transport B, wait for result."""
    command_id = await vq_data.enqueue_command(guild_id, method, path, query, body)
    result = await _wait_for_command_result(guild_id, command_id)
    return result


async def _transport_a_or_b_call(guild_id: int, method: str, path: str,
                                  query: str = "", body: Any = None) -> dict:
    """Switch between transport A (direct HTTP) and B (queue + wait).

    Returns dict with at least one of:
      {status: 200, result: {...}}  — success (or HTTP error from mod)
      {error: "timeout", message: ...}  — transport B timeout
      {error: "server_unavailable", message: ...}  — transport A connection refused
      {error: "not_configured", message: ...}  — no api_url set
    """
    transport = await vq_data.get_transport(guild_id)
    if transport == "A":
        client = await VQClient.from_guild(guild_id)
        if client is None:
            return {"error": "not_configured",
                    "message": "Transport A: vq_api_url not set. Configure on /tokens."}
        try:
            data = await client._request(method, path, query=query, body=body)
            return {"status": 200, "result": data}
        except VQError as e:
            return {"status": e.status, "error": e.code, "message": e.message,
                    "result": e.body}
        except VQServerUnavailable as e:
            return {"error": "server_unavailable",
                    "message": f"MC server unreachable (sleeping?): {e}"}
    else:  # transport B
        return await _transport_b_call(guild_id, method, path, query, body)


# === Unified API surface ===

async def health(guild_id: int) -> dict:
    """GET /api/health — server status."""
    return await _transport_a_or_b_call(guild_id, "GET", "/api/health")


async def list_players(guild_id: int, *, all_players: bool = False) -> dict:
    q = "all=true" if all_players else ""
    return await _transport_a_or_b_call(guild_id, "GET", "/api/players", query=q)


async def get_player(guild_id: int, uuid: str, *, locale: str = "") -> dict:
    q = f"locale={locale}" if locale else ""
    return await _transport_a_or_b_call(guild_id, "GET", f"/api/players/{uuid}", query=q)


async def get_player_by_discord(guild_id: int, discord_id: int) -> dict:
    return await _transport_a_or_b_call(
        guild_id, "GET", f"/api/players/by-discord/{int(discord_id)}")


async def list_villages(guild_id: int, *, limit: int = 200) -> dict:
    return await _transport_a_or_b_call(
        guild_id, "GET", "/api/villages", query=f"limit={int(limit)}")


async def list_caravans(guild_id: int) -> dict:
    return await _transport_a_or_b_call(guild_id, "GET", "/api/caravans")


async def economy_summary(guild_id: int) -> dict:
    return await _transport_a_or_b_call(guild_id, "GET", "/api/economy/summary")


async def leaderboard(guild_id: int, type_: str, *, limit: int = 10) -> dict:
    return await _transport_a_or_b_call(
        guild_id, "GET", f"/api/leaderboards/{type_}", query=f"limit={int(limit)}")


async def list_events(guild_id: int, after_id: int = 0, *, limit: int = 100) -> dict:
    return await _transport_a_or_b_call(
        guild_id, "GET", "/api/events",
        query=f"after_id={int(after_id)}&limit={int(limit)}")


async def list_quests(guild_id: int) -> dict:
    return await _transport_a_or_b_call(guild_id, "GET", "/api/quests")


async def get_quest(guild_id: int, quest_id: str) -> dict:
    return await _transport_a_or_b_call(guild_id, "GET", f"/api/quests/{quest_id}")


async def create_quest(guild_id: int, quest_body: dict) -> dict:
    return await _transport_a_or_b_call(guild_id, "POST", "/api/quests", body=quest_body)


async def update_quest(guild_id: int, short_id: str, patch: dict) -> dict:
    return await _transport_a_or_b_call(
        guild_id, "PATCH", f"/api/quests/custom/{short_id}", body=patch)


async def delete_quest(guild_id: int, short_id: str) -> dict:
    return await _transport_a_or_b_call(
        guild_id, "DELETE", f"/api/quests/custom/{short_id}")


async def claim_link(guild_id: int, code: str, discord_id: int,
                     discord_username: str) -> dict:
    """POST /api/links/claim — bind Discord ID to MC account via 6-digit code."""
    body = {
        "code": str(code),
        "discord_id": int(discord_id),
        "discord_username": str(discord_username),
    }
    result = await _transport_a_or_b_call(guild_id, "POST", "/api/links/claim", body=body)
    # If success, also cache the link locally for fast /vqme lookups
    if result.get("status") == 200 and isinstance(result.get("result"), dict):
        link = result["result"].get("link")
        if link:
            try:
                await vq_data.upsert_link(guild_id, link)
            except Exception as e:
                log.warning("Failed to cache link for guild %s: %s", guild_id, e)
    return result


async def get_link(guild_id: int, discord_id: int) -> dict:
    return await _transport_a_or_b_call(
        guild_id, "GET", f"/api/links/{int(discord_id)}")


async def delete_link(guild_id: int, discord_id: int) -> dict:
    """DELETE /api/links/{discord_id} — unlink."""
    result = await _transport_a_or_b_call(
        guild_id, "DELETE", f"/api/links/{int(discord_id)}")
    # Remove from local cache regardless of result
    try:
        await vq_data.delete_link(guild_id, discord_id)
    except Exception as e:
        log.warning("Failed to remove link cache for guild %s: %s", guild_id, e)
    return result


# === Config helpers ===

async def get_config(guild_id: int) -> dict:
    """Return current VQ config for the guild (tokens masked for safety)."""
    transport = await vq_data.get_transport(guild_id)
    api_url = await vq_data.get_api_url(guild_id)
    server_id = await vq_data.get_server_id(guild_id)
    api_token = await vq_data.get_api_token(guild_id)
    gateway_token = await vq_data.get_gateway_token(guild_id)
    last_event_id = await vq_data.get_last_event_id(guild_id)
    last_command_id = await vq_data.get_last_command_id(guild_id)
    return {
        "transport": transport,
        "api_url": api_url,
        "api_token_set": bool(api_token),
        "gateway_token_set": bool(gateway_token),
        "server_id": server_id,
        "last_event_id": last_event_id,
        "last_command_id": last_command_id,
    }


async def save_config(guild_id: int, *, transport: Optional[str] = None,
                      api_url: Optional[str] = None,
                      api_token: Optional[str] = None,
                      gateway_token: Optional[str] = None,
                      server_id: Optional[str] = None) -> dict:
    """Update VQ config. Only non-None fields are updated.

    For api_token/gateway_token: empty string clears the field, non-empty
    string sets it (encrypting). Pass None to leave unchanged."""
    if transport is not None:
        if transport not in ("A", "B"):
            return {"error": "bad_request", "message": "transport must be 'A' or 'B'"}
        await vq_data.set_transport(guild_id, transport)
    if api_url is not None:
        await vq_data.set_api_url(guild_id, api_url)
    if api_token is not None:
        await vq_data.set_api_token(guild_id, api_token)
    if gateway_token is not None:
        await vq_data.set_gateway_token(guild_id, gateway_token)
    if server_id is not None:
        try:
            await vq_data.set_server_id(guild_id, server_id)
        except ValueError as e:
            # v3.1.1: server_id regex validation failed
            return {"error": "bad_request", "message": str(e)}
    return await get_config(guild_id)


async def test_connection(guild_id: int) -> dict:
    """Try GET /api/health to verify the connection.

    For transport A: makes direct HTTP call.
    For transport B: enqueues a health command (mod will pick up on next poll).
    Returns {ok: bool, message: str, ...}."""
    result = await health(guild_id)
    if result.get("status") == 200 and isinstance(result.get("result"), dict):
        return {"ok": True, "message": "Connected", "health": result["result"]}
    if result.get("error") == "timeout":
        return {"ok": False, "message": "Timeout — mod not polling (server sleeping?)"}
    if result.get("error") == "server_unavailable":
        return {"ok": False, "message": result.get("message", "Server unreachable")}
    if result.get("error") == "not_configured":
        return {"ok": False, "message": result.get("message", "Not configured")}
    # HTTP error from mod
    return {"ok": False,
            "message": f"HTTP {result.get('status')}: {result.get('message', 'unknown')}"}
