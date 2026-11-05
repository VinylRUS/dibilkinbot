"""Village Quest — panel routes (HTML page + REST API for AJAX).

v3.1

This module registers:
  - GET  /vq                              — main page (admin only)
  - GET  /api/vq/health                    — test connection
  - GET  /api/vq/players                   — list online players (or all)
  - GET  /api/vq/players/{uuid}            — player card
  - GET  /api/vq/players/by-discord/{id}   — player by Discord ID
  - GET  /api/vq/villages
  - GET  /api/vq/caravans
  - GET  /api/vq/economy
  - GET  /api/vq/leaderboards/{type}
  - GET  /api/vq/quests
  - GET  /api/vq/quests/{id}
  - POST /api/vq/quests                    — create custom quest
  - PATCH /api/vq/quests/custom/{id}      — update
  - DELETE /api/vq/quests/custom/{id}    — delete
  - GET  /api/vq/events?after_id=
  - POST /api/vq/links/claim              — link via code (also via /linkmc slash)
  - GET  /api/vq/links/{discord_id}
  - DELETE /api/vq/links/{discord_id}
  - GET  /api/vq/config                    — current config (masked)
  - POST /api/vq/config                    — save config (called from /tokens page)
  - POST /api/vq/test_connection          — quick GET /api/health check

All endpoints require admin (Матка or Трутень) auth.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

import vq_api
import vq_data
from deps import (
    get_current_guild_id,
    require_admin,
    templates,
)

log = logging.getLogger("vq_routes")
router = APIRouter()


# === HTML page ===

@router.get("/vq", response_class=HTMLResponse)
async def vq_page(request: Request, _user: dict = Depends(require_admin)):
    """Main VQ integration page — players, villages, caravans, economy, quests, events.

    Transport-agnostic: page makes AJAX calls to /api/vq/* which auto-switch
    between transport A (direct HTTP) and B (queue + wait).
    """
    guild_id = get_current_guild_id(_user)
    config = await vq_api.get_config(guild_id)
    return templates.TemplateResponse(request, "vq.html", {
        "user": _user,
        "vq_config": config,
        "current_guild_id": guild_id,
        "active": "vq",  # for sidebar.html highlight
    })


# === Helpers ===

def _json(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


def _translate_call_result(result: dict) -> JSONResponse:
    """Translate the unified API result to an HTTP response for the panel client.

    - {status: 200, result: X} → 200 OK with X
    - {status: NNN, ...}       → NNN with error body
    - {error: "timeout"}      → 504 Gateway Timeout
    - {error: "server_unavailable"} → 503 Service Unavailable
    - {error: "not_configured"} → 424 Failed Dependency
    """
    if "status" in result:
        st = result["status"]
        if st == 200:
            return _json(result.get("result") or {"ok": True})
        # Error from the mod itself
        return _json({
            "error": result.get("error") or f"http_{st}",
            "message": result.get("message") or "mod error",
            "details": result.get("result"),
        }, status_code=st)
    # Local error (timeout / not configured / etc)
    err = result.get("error") or "internal_error"
    msg = result.get("message") or "unknown error"
    if err == "timeout":
        return _json({"error": err, "message": msg}, status_code=504)
    if err == "server_unavailable":
        return _json({"error": err, "message": msg}, status_code=503)
    if err == "not_configured":
        return _json({"error": err, "message": msg}, status_code=424)
    return _json({"error": err, "message": msg}, status_code=500)


# === Config endpoints (called from /tokens page) ===

@router.get("/api/vq/config")
async def api_get_config(_user: dict = Depends(require_admin)):
    """Get current VQ config (tokens masked — only 'set' flags exposed)."""
    guild_id = get_current_guild_id(_user)
    config = await vq_api.get_config(guild_id)
    return _json(config)


@router.post("/api/vq/config")
async def api_save_config(
    _user: dict = Depends(require_admin),
    transport: Optional[str] = Form(None),
    api_url: Optional[str] = Form(None),
    api_token: Optional[str] = Form(None),
    gateway_token: Optional[str] = Form(None),
    server_id: Optional[str] = Form(None),
):
    """Save VQ config. Only non-None fields are updated (Form default None).
    For tokens: pass empty string to clear, non-empty to set, omit (None) to keep."""
    guild_id = get_current_guild_id(_user)
    result = await vq_api.save_config(
        guild_id,
        transport=transport,
        api_url=api_url,
        api_token=api_token,
        gateway_token=gateway_token,
        server_id=server_id,
    )
    if "error" in result:
        return _json(result, status_code=400)
    log.info("VQ config updated for guild %s by user %s (transport=%s)",
             guild_id, _user.get("discord_id"), result.get("transport"))
    return _json(result)


@router.post("/api/vq/test_connection")
async def api_test_connection(_user: dict = Depends(require_admin)):
    """Quick GET /api/health check."""
    guild_id = get_current_guild_id(_user)
    result = await vq_api.test_connection(guild_id)
    return _json(result, status_code=200 if result.get("ok") else 424)


# === Mod API proxies (auto-switch transport A/B) ===

@router.get("/api/vq/health")
async def api_health(_user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.health(guild_id))


@router.get("/api/vq/players")
async def api_list_players(_user: dict = Depends(require_admin), all: bool = False):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.list_players(guild_id, all_players=all))


@router.get("/api/vq/players/{uuid}")
async def api_get_player(uuid: str, _user: dict = Depends(require_admin),
                          locale: str = ""):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.get_player(guild_id, uuid, locale=locale))


@router.get("/api/vq/players/by-discord/{discord_id}")
async def api_get_player_by_discord(discord_id: int, _user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.get_player_by_discord(guild_id, discord_id))


@router.get("/api/vq/villages")
async def api_list_villages(_user: dict = Depends(require_admin), limit: int = 200):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.list_villages(guild_id, limit=limit))


@router.get("/api/vq/caravans")
async def api_list_caravans(_user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.list_caravans(guild_id))


@router.get("/api/vq/economy")
async def api_economy(_user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.economy_summary(guild_id))


@router.get("/api/vq/leaderboards/{type_}")
async def api_leaderboard(type_: str, _user: dict = Depends(require_admin), limit: int = 10):
    if type_ not in ("wealth", "reputation", "quests_total",
                     "route_successes", "weekly_completed"):
        return _json({"error": "bad_request", "message": f"unknown leaderboard type {type_!r}"},
                     status_code=400)
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.leaderboard(guild_id, type_, limit=limit))


@router.get("/api/vq/quests")
async def api_list_quests(_user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.list_quests(guild_id))


@router.get("/api/vq/quests/{quest_id}")
async def api_get_quest(quest_id: str, _user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    return _translate_call_result(await vq_api.get_quest(guild_id, quest_id))


@router.post("/api/vq/quests")
async def api_create_quest(request: Request, _user: dict = Depends(require_admin)):
    """Create a custom quest. Body = quest JSON (rest_api.md §9 schema)."""
    guild_id = get_current_guild_id(_user)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "bad_request", "message": "expected JSON body"},
                     status_code=400)
    if not isinstance(body, dict):
        return _json({"error": "bad_request", "message": "body must be a JSON object"},
                     status_code=400)
    log.info("User %s creating VQ quest %s for guild %s",
             _user.get("discord_id"), body.get("id"), guild_id)
    return _translate_call_result(await vq_api.create_quest(guild_id, body))


@router.patch("/api/vq/quests/custom/{short_id}")
async def api_update_quest(short_id: str, request: Request,
                            _user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    try:
        patch = await request.json()
    except Exception:
        return _json({"error": "bad_request", "message": "expected JSON body"},
                     status_code=400)
    if not isinstance(patch, dict):
        return _json({"error": "bad_request", "message": "body must be a JSON object"},
                     status_code=400)
    log.info("User %s updating VQ quest %s for guild %s",
             _user.get("discord_id"), short_id, guild_id)
    return _translate_call_result(await vq_api.update_quest(guild_id, short_id, patch))


@router.delete("/api/vq/quests/custom/{short_id}")
async def api_delete_quest(short_id: str, _user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    log.info("User %s deleting VQ quest %s for guild %s",
             _user.get("discord_id"), short_id, guild_id)
    return _translate_call_result(await vq_api.delete_quest(guild_id, short_id))


# === Events ===

@router.get("/api/vq/events")
async def api_list_events(_user: dict = Depends(require_admin),
                          after_id: int = 0, limit: int = 100,
                          type: Optional[str] = None):
    """List events received from mod (transport A: direct, transport B: from local DB).

    For transport B we read from local cache (events were pushed by mod via
    POST /vq/events). For transport A we proxy to mod's /api/events endpoint.

    Type filter is only supported for transport B (local DB query)."""
    guild_id = get_current_guild_id(_user)
    transport = await vq_data.get_transport(guild_id)
    if transport == "B":
        events = await vq_data.list_events(guild_id, after_id=after_id,
                                            limit=limit, event_type=type)
        return _json({"count": len(events), "events": events,
                      "last_id": await vq_data.get_max_event_id(guild_id)})
    return _translate_call_result(await vq_api.list_events(
        guild_id, after_id, limit=limit))


# === Links ===

@router.post("/api/vq/links/claim")
async def api_claim_link(request: Request, _user: dict = Depends(require_admin)):
    """POST /api/links/claim — bind Discord ID to MC account via 6-digit code.

    Body: {code: "123456", discord_id: 123, discord_username: "name"}
    """
    guild_id = get_current_guild_id(_user)
    try:
        body = await request.json()
    except Exception:
        return _json({"error": "bad_request", "message": "expected JSON body"},
                     status_code=400)
    code = str(body.get("code") or "").strip()
    discord_id = body.get("discord_id")
    discord_username = str(body.get("discord_username") or "")
    if not code or not discord_id:
        return _json({"error": "bad_request",
                       "message": "fields 'code' and 'discord_id' are required"},
                     status_code=400)
    try:
        discord_id_int = int(discord_id)
    except (ValueError, TypeError):
        return _json({"error": "bad_request", "message": "discord_id must be an integer"},
                     status_code=400)
    return _translate_call_result(await vq_api.claim_link(
        guild_id, code, discord_id_int, discord_username))


@router.get("/api/vq/links/{discord_id}")
async def api_get_link(discord_id: int, _user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    # Try local cache first (faster for transport B, always populated on claim)
    cached = await vq_data.get_link_by_discord(guild_id, discord_id)
    if cached:
        return _json({"link": cached, "cached": True})
    return _translate_call_result(await vq_api.get_link(guild_id, discord_id))


@router.delete("/api/vq/links/{discord_id}")
async def api_delete_link(discord_id: int, _user: dict = Depends(require_admin)):
    guild_id = get_current_guild_id(_user)
    log.info("User %s unlinking discord_id=%s for guild %s",
             _user.get("discord_id"), discord_id, guild_id)
    return _translate_call_result(await vq_api.delete_link(guild_id, discord_id))
