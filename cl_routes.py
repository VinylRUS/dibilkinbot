"""FastAPI APIRouter — маршруты ChaosLedger (web-панель /ledger + JSON API).

Доступ: только Матка (superuser) — `require_superuser` на каждом endpoint.
Realtime: `/api/ledger/state` теперь отдаёт состояние из `cl_cache` —
in-memory кеша, обновляемого модом через WS reverse-tunnel (push каждые ~10
секунд, без опроса мода по HTTP). Бот больше не делает исходящих HTTP-запросов
к моду — соединение однонаправленное в обратную сторону (мод → бот).

Маршруты:
  GET  /ledger               — HTML-страница (sidebar + realtime polling)
  GET  /api/ledger/state     — snapshot из cl_cache (обновляется push'ом мода)
  GET  /api/ledger/health    — liveness мода (из cl_cache, без HTTP к моду)
  GET  /api/ledger/config    — текущий config (api_key как masked)
  POST /api/ledger/config    — обновить config (api_url, api_key, enabled)
  GET  /api/ledger/links     — список привязок Discord↔MC (админ-таблица)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

import cl_cache
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
    """Snapshot состояния из cl_cache — обновляется push'ом мода через WS.

    Мод сам отправляет snapshot каждые ~10 секунд (см. WsTunnelClient в репо
    мода). Бот ничего не опрашивает по HTTP — это и есть смысл reverse-tunnel.
    Поле `cached: true` всегда, т.к. данные из push-кеша.
    """
    state = cl_cache.get_state()
    return JSONResponse(state)


@router.get("/api/ledger/health")
async def api_ledger_health(
    request: Request, user: dict = Depends(require_superuser)
) -> JSONResponse:
    """Liveness мода — для индикатора в шапке страницы.

    Если reverse-tunnel активен (мод подключился и шлёт snapshot'ы),
    отдаём cached health из cl_cache. Иначе ok=false, error=mod_disconnected.
    """
    return JSONResponse(cl_cache.get_health())


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
