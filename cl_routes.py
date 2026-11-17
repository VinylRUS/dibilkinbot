"""FastAPI APIRouter — маршруты ChaosLedger (web-панель /ledger + JSON API).

Доступ: только Матка (superuser) — `require_superuser` на каждом endpoint.
Realtime: `/api/ledger/state` опрашивается каждые 10 сек из templates/ledger.html.
Bot-side кеш snapshot TTL 10 сек в `cl_client.CLClient.get_ledger_state()` —
несколько Маток получают один snapshot, однопоточный executor мода не
бомбардируется параллельными одинаковыми запросами.

Маршруты:
  GET  /ledger               — HTML-страница (sidebar + realtime polling)
  GET  /api/ledger/state     — агрегированный snapshot (для setInterval 10с)
  GET  /api/ledger/health    — liveness мода (индикатор в шапке)
  GET  /api/ledger/config    — текущий config (api_key как masked)
  POST /api/ledger/config    — обновить config (api_url, api_key, enabled)
  GET  /api/ledger/links     — список привязок Discord↔MC (админ-таблица)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

import cl_client
import cl_data
from deps import JSONResponse, require_superuser, templates

log = logging.getLogger(__name__)
router = APIRouter()


def _gid(user: dict) -> int:
    """Текущая гильдия юзера из сессии."""
    return int(user.get("current_guild_id") or 0)


# === HTML страница ==========================================================


@router.get("/ledger", response_class=HTMLResponse)
async def ledger_page(
    request: Request, user: dict = Depends(require_superuser)
) -> HTMLResponse:
    """Страница /ledger — realtime-просмотр экономики/рынка/квестов/аудита.

    Config передаётся в template, но api_key там не выводится — только
    индикатор «ключ задан/не задан».
    """
    guild_id = _gid(user)
    cfg = await cl_data.get_cl_config(guild_id)
    return templates.TemplateResponse(
        request,
        "ledger.html",
        {
            "user": user,
            "active": "ledger",
            "cl_config": cfg,  # {enabled, api_url, api_key}
            "cl_api_key_set": bool(cfg.get("api_key")),
        },
    )


# === JSON API (для polling 10с) =============================================


@router.get("/api/ledger/state")
async def api_ledger_state(
    request: Request, user: dict = Depends(require_superuser)
) -> JSONResponse:
    """Агрегированный snapshot: health + top + market + transactions + audit + quests.

    Кеш TTL 10 сек в cl_client.CLClient — повторные запросы от разных Маток
    получают один snapshot. Поле `cached: true` если ответ из кеша.
    """
    guild_id = _gid(user)
    client = await cl_client.get_client(guild_id)
    state = await client.get_ledger_state()
    return JSONResponse(state)


@router.get("/api/ledger/health")
async def api_ledger_health(
    request: Request, user: dict = Depends(require_superuser)
) -> JSONResponse:
    """Liveness мода — для индикатора в шапке страницы."""
    guild_id = _gid(user)
    client = await cl_client.get_client(guild_id)
    if not client.configured:
        return JSONResponse({"ok": False, "error": "not_configured"})
    try:
        h = await client.health()
        return JSONResponse({"ok": True, "health": h})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# === Config ================================================================


@router.get("/api/ledger/config")
async def api_ledger_config(
    request: Request, user: dict = Depends(require_superuser)
) -> JSONResponse:
    """Текущий config. api_key возвращается как masked (есть/нет), не сам ключ."""
    guild_id = _gid(user)
    cfg = await cl_data.get_cl_config(guild_id)
    return JSONResponse(
        {
            "enabled": cfg["enabled"],
            "api_url": cfg["api_url"],
            "api_key_set": bool(cfg["api_key"]),
        }
    )


@router.post("/api/ledger/config")
async def api_ledger_config_set(
    request: Request,
    user: dict = Depends(require_superuser),
    api_url: str = Form(default=""),
    api_key: str = Form(default=""),
    enabled: str = Form(default=""),
) -> JSONResponse:
    """Обновить config.

    Пустой api_key = явный open mode мода (не «не менять»). Чтобы не трогать
    ключ — не передавайте поле совсем (но HTML-форма всегда его шлёт; для
    UX в template есть чекбокс «сменить ключ»).
    """
    guild_id = _gid(user)
    kwargs: dict = {}
    # api_url: обновляем только если пришло непустое (пустая строка = удалить —
    # слишком опасно как default формы).
    url = (api_url or "").strip()
    if url:
        kwargs["api_url"] = url
    # api_key: всегда обновляем (пустой = open mode). Это осмысленное значение.
    kwargs["api_key"] = (api_key or "").strip()
    # enabled
    if enabled in ("0", "1", "true", "false"):
        kwargs["enabled"] = enabled in ("1", "true")
    cfg = await cl_data.set_cl_config(guild_id, **kwargs)
    return JSONResponse(
        {
            "ok": True,
            "enabled": cfg["enabled"],
            "api_url": cfg["api_url"],
            "api_key_set": bool(cfg["api_key"]),
        }
    )


# === Links =================================================================


@router.get("/api/ledger/links")
async def api_ledger_links(
    request: Request, user: dict = Depends(require_superuser)
) -> JSONResponse:
    """Список привязок Discord↔Minecraft для текущей гильдии."""
    guild_id = _gid(user)
    links = await cl_data.list_links(guild_id)
    return JSONResponse({"links": links, "count": len(links)})
