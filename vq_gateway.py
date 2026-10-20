"""Village Quest — Gateway endpoints for Transport B (mod → panel).

When the guild is configured with transport='B' (the default for exarathon
— hosts that don't allow incoming ports), the mod polls the panel for
commands and pushes events via 3 endpoints.

This module registers 3 FastAPI routes:

  ① GET  /vq/commands?after_id=&server_id=&online=&mod_version=
       Returns queued commands (virtual HTTP requests) for the mod to execute.

  ② POST /vq/results
       Mod sends back the results of executed commands (idempotent by command_id).

  ③ POST /vq/events
       Mod pushes events (batches up to push_batch). Panel must ack with
       {"acked_event_id": last_id} so mod continues from that cursor.

All 3 endpoints require `Authorization: Bearer <gateway_token>` header.
Unknown server_id → 403.

See rest_api.md §4 in the mod repo for the contract.

v3.1
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse

import vq_data
from deps import require_user  # noqa: F401  (kept for parity, but gateway uses token auth)

log = logging.getLogger("vq_gateway")

router = APIRouter()


def _find_guild_by_server_id(server_id: str) -> int | None:
    """Find guild_id by stored server_id. We don't index server_id globally —
    iterate approved guilds and check vq_server_id setting for each.
    Returns None if no match.

    For multi-guild setups this is O(N) per request — fine for small N.
    A dedicated server_id → guild_id index table can be added if needed."""
    import asyncio
    import db

    async def _scan():
        try:
            guild_ids = await db.list_approved_guilds()
        except Exception as e:
            log.warning("list_approved_guilds failed: %s", e)
            return None
        for gid in guild_ids:
            try:
                stored_sid = await vq_data.get_server_id(gid)
                if stored_sid == server_id:
                    return gid
            except Exception:
                continue
        return None

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're being called from within an async context — caller should
            # use _find_guild_by_server_id_async instead. Fallback to sync scan
            # via asyncio.run is not safe. Return None — caller must use async.
            return None
        return loop.run_until_complete(_scan())
    except RuntimeError:
        # No event loop — return None, caller must use async variant
        return None


async def _find_guild_by_server_id_async(server_id: str) -> int | None:
    """Async version of _find_guild_by_server_id. Returns guild_id if found,
    None otherwise. Also returns debug info via the global _last_scan_debug
    so callers can log WHY the lookup failed."""
    import db
    global _last_scan_debug
    _last_scan_debug = {
        "queried_server_id": server_id,
        "scanned_guilds": [],
    }
    try:
        guild_ids = await db.list_approved_guilds()
    except Exception as e:
        log.warning("list_approved_guilds failed: %s", e)
        _last_scan_debug["error"] = f"list_approved_guilds: {e}"
        return None
    for gid in guild_ids:
        try:
            stored_sid = await vq_data.get_server_id(gid)
            _last_scan_debug["scanned_guilds"].append({
                "guild_id": gid,
                "stored_server_id": stored_sid,
                "match": stored_sid == server_id,
            })
            if stored_sid == server_id:
                return gid
        except Exception as e:
            _last_scan_debug["scanned_guilds"].append({
                "guild_id": gid,
                "error": str(e),
            })
            continue
    return None


# v3.1.3: debugging info from the last _find_guild_by_server_id_async call.
# Used by _verify_token to log WHY auth failed (so user can see in bot logs
# whether server_id didn't match vs. gateway_token mismatch).
_last_scan_debug: dict = {}


async def _verify_token(authorization: str | None, server_id: str) -> tuple[int | None, str]:
    """Verify Bearer token matches stored gateway_token for the guild identified
    by server_id.

    v3.1.3: returns (guild_id, reason) — reason is a human-readable string
    describing why auth failed (or "ok" on success). Helps debugging 403 errors.

    Reasons:
    - "ok" — auth succeeded, guild_id is the matched guild
    - "missing_authorization" — no Authorization header
    - "invalid_scheme" — Authorization doesn't start with "Bearer "
    - "empty_token" — Bearer prefix but no token
    - "unknown_server_id" — server_id not found in any guild
    - "invalid_gateway_token" — server_id found but token doesn't match
    """
    global _last_scan_debug
    if not authorization:
        return None, "missing_authorization"
    if not authorization.startswith("Bearer "):
        return None, "invalid_scheme"
    token = authorization[7:].strip()
    if not token:
        return None, "empty_token"
    guild_id = await _find_guild_by_server_id_async(server_id)
    if guild_id is None:
        scanned = _last_scan_debug.get("scanned_guilds", [])
        log.warning("VQ gateway auth failed: unknown server_id=%r. Scanned %d guilds: %s",
                    server_id, len(scanned),
                    [{"guild_id": g.get("guild_id"),
                      "stored": g.get("stored_server_id"),
                      "match": g.get("match")} for g in scanned])
        return None, "unknown_server_id"
    stored = await vq_data.get_gateway_token(guild_id)
    if not stored:
        log.warning("VQ gateway auth failed: guild %s has no gateway_token set "
                   "(configure on /tokens → VQ block)", guild_id)
        return None, "guild_missing_gateway_token"
    if stored != token:
        log.warning("VQ gateway auth failed: gateway_token mismatch for guild %s "
                   "(expected %d chars, got %d chars)",
                   guild_id, len(stored), len(token))
        return None, "invalid_gateway_token"
    return guild_id, "ok"


def _unauthorized(reason: str = "missing or invalid bearer token") -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized", "message": reason, "reason": reason},
        status_code=401,
    )


def _forbidden(reason: str, server_id: str = "") -> JSONResponse:
    """v3.1.3: returns 403 with SPECIFIC reason — was generic 'unknown server_id'
    for ALL failures (missing token / bad token / unknown server_id / token mismatch).
    Now exposes the real reason so user can debug."""
    msg_map = {
        "unknown_server_id": f"server_id {server_id!r} not registered in any guild",
        "invalid_gateway_token": f"gateway_token mismatch for server_id {server_id!r}",
        "guild_missing_gateway_token": f"guild has no gateway_token configured (set on /tokens)",
        "invalid_scheme": "Authorization header must be 'Bearer <token>'",
        "missing_authorization": "missing Authorization header",
        "empty_token": "Bearer token is empty",
    }
    message = msg_map.get(reason, f"auth failed: {reason}")
    return JSONResponse(
        {"error": "forbidden", "message": message, "reason": reason,
         "server_id": server_id},
        status_code=403,
    )


# === ① GET /vq/commands — mod polls for commands ===

@router.get("/vq/commands")
async def vq_get_commands(
    request: Request,
    after_id: int = 0,
    server_id: str = "",
    online: int = 0,
    mod_version: str = "",
    authorization: str | None = Header(None),
):
    """Mod polls every poll_seconds (this is also the heartbeat).

    Returns pending commands with id > after_id and status IS NULL.
    Body shape: rest_api.md §4.1 ①."""
    if not server_id:
        return _unauthorized("missing server_id query param")
    guild_id, reason = await _verify_token(authorization, server_id)
    if guild_id is None:
        # v3.1.3: distinguish missing auth (401) vs forbidden (403)
        if reason in ("missing_authorization", "invalid_scheme", "empty_token"):
            return _unauthorized(reason)
        return _forbidden(reason, server_id)

    try:
        commands = await vq_data.list_pending_commands(guild_id, after_id=after_id, limit=100)
        # Mark commands as sent (sent_at timestamp) — idempotent
        for cmd in commands:
            await vq_data.mark_command_sent(guild_id, cmd["id"])

        max_cmd = await vq_data.get_max_command_id(guild_id)
        return JSONResponse({
            "commands": commands,
            "last_command_id": max_cmd,
        })
    except Exception as e:
        log.warning("vq_get_commands failed for guild %s: %s", guild_id, e, exc_info=True)
        return JSONResponse(
            {"error": "internal_error", "message": str(e)},
            status_code=500,
        )


# === ② POST /vq/results — mod sends results ===

@router.post("/vq/results")
async def vq_post_results(
    request: Request,
    authorization: str | None = Header(None),
):
    """Mod sends back results (batches with possible duplicates — dedup by command_id).

    Body shape: rest_api.md §4.1 ②.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "bad_request", "message": "expected JSON body"},
            status_code=400,
        )
    server_id = body.get("server_id", "")
    guild_id, reason = await _verify_token(authorization, server_id)
    if guild_id is None:
        if reason in ("missing_authorization", "invalid_scheme", "empty_token"):
            return _unauthorized(reason)
        return _forbidden(reason, server_id or "")

    results = body.get("results") or []
    if not isinstance(results, list):
        return JSONResponse(
            {"error": "bad_request", "message": "results must be a list"},
            status_code=400,
        )

    stored = 0
    for r in results:
        try:
            command_id = int(r.get("command_id") or 0)
            if command_id <= 0:
                continue
            status_int = int(r.get("status") or 0)
            result_body = r.get("body")
            # store_command_result is idempotent by command_id — returns True
            # only on first insert
            newly = await vq_data.store_command_result(
                guild_id, command_id, status_int, result_body
            )
            if newly:
                stored += 1
        except Exception as e:
            log.warning("Failed to store result cmd=%s: %s",
                        r.get("command_id"), e)

    log.debug("vq_post_results: guild=%s, received=%d, newly_stored=%d",
              guild_id, len(results), stored)
    return JSONResponse({"ok": True, "stored": stored})


# === ③ POST /vq/events — mod pushes events ===

@router.post("/vq/events")
async def vq_post_events(
    request: Request,
    authorization: str | None = Header(None),
):
    """Mod pushes events (batches up to push_batch). Panel MUST ack with
    {"acked_event_id": last_event_id} so mod continues from that cursor.

    Body shape: rest_api.md §4.1 ③.

    first_event_id lets panel notice a journal reset (id jumped back to 1) —
    in that case we just continue from the new id.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "bad_request", "message": "expected JSON body"},
            status_code=400,
        )
    server_id = body.get("server_id", "")
    guild_id, reason = await _verify_token(authorization, server_id)
    if guild_id is None:
        if reason in ("missing_authorization", "invalid_scheme", "empty_token"):
            return _unauthorized(reason)
        return _forbidden(reason, server_id or "")

    events = body.get("events") or []
    if not isinstance(events, list):
        return JSONResponse(
            {"error": "bad_request", "message": "events must be a list"},
            status_code=400,
        )

    first_event_id = int(body.get("first_event_id") or 0)
    last_event_id = int(body.get("last_event_id") or 0)
    last_cursor = await vq_data.get_last_event_id(guild_id)

    # Detect journal reset (id jumped back) — if first_event_id <= last_cursor,
    # the mod's journal was reset and we should just continue with new ids.
    if first_event_id and last_cursor and first_event_id <= last_cursor:
        log.info("Guild %s: VQ event journal reset detected (first=%s <= cursor=%s) — continuing",
                 guild_id, first_event_id, last_cursor)

    new_max = last_event_id
    new_count = 0
    for ev in events:
        try:
            ev_id = int(ev.get("id") or 0)
            if ev_id <= 0:
                continue
            inserted = await vq_data.append_event(guild_id, ev)
            if inserted:
                new_count += 1
            if ev_id > new_max:
                new_max = ev_id
        except Exception as e:
            log.warning("Failed to store event id=%s: %s", ev.get("id"), e)

    # Ack the highest event_id we've seen (whether new or duplicate) —
    # mod continues from this id on next poll.
    if new_max > 0:
        await vq_data.set_last_event_id(guild_id, new_max)

    log.debug("vq_post_events: guild=%s, received=%d, newly_stored=%d, ack=%d",
              guild_id, len(events), new_count, new_max)
    return JSONResponse({"acked_event_id": new_max, "stored": new_count})
