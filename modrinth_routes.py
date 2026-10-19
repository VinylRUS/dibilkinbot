"""FastAPI APIRouter — маршруты для подписок на модпаки Modrinth.

v2.4.1: веб-интерфейс для управления подписками на модпаки.
Доступ: require_admin (Матка + Трутень).

Содержит:
- HTML-страницу /modpacks (админка)
- REST API /api/modpacks/* (list, add, remove, check_now, search)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

import db
import modrinth
import modrinth_monitor
from modrinth import (
    ModrinthError,
    ProjectNotFoundError,
    UnauthorizedError,
    format_download_url,
    format_filesize,
    format_version_type_emoji,
    get_primary_file,
    parse_project_id_from_url_or_input,
)

from deps import (
    get_current_guild_id,
    require_admin,
    templates,
)

router = APIRouter()
_log = logging.getLogger("modrinth_routes")


# === HTML-страница ===

@router.get("/modpacks", response_class=HTMLResponse)
async def modpacks_page(request: Request, _user: dict = Depends(require_admin)):
    """Страница управления подписками на модпаки. Только админ."""
    guild_id = get_current_guild_id(_user)
    subs = await db.g_list_modrinth_subs(guild_id)
    # Канал для постинга
    channel_id = await db.get_setting("channel_modpacks_id") or ""
    return templates.TemplateResponse(request, "modpacks.html", {
        "user": _user,
        "subs": subs,
        "channel_modpacks_id": channel_id,
        "saved": request.query_params.get("saved") == "1",
    })


# === REST API ===

@router.get("/api/modpacks")
async def api_list_modpacks(_user: dict = Depends(require_admin)):
    """Список всех подписок текущей гильдии."""
    guild_id = get_current_guild_id(_user)
    subs = await db.g_list_modrinth_subs(guild_id)
    return JSONResponse({"subs": subs, "count": len(subs)})


@router.post("/api/modpacks/add")
async def api_add_modpack(
    request: Request,
    _user: dict = Depends(require_admin),
    project: str = Form(...),
):
    """Добавить подписку на модпак.

    Принимает:
    - bare slug/ID: 'cobblemon-fabric' или '5FFgwNNP'
    - URL modrinth.com/modpack/<slug>
    - URL cdn.modrinth.com/data/<id>/versions/<ver>/<file>
    """
    guild_id = get_current_guild_id(_user)
    added_by = _user.get("discord_id", 0)

    project_id_or_slug = parse_project_id_from_url_or_input(project)
    if not project_id_or_slug:
        return JSONResponse(
            {"error": "Не удалось распознать slug или ID проекта из ввода."},
            status_code=400,
        )

    # Получаем инфу о проекте с Modrinth
    try:
        info = await modrinth.get_project(project_id_or_slug)
    except ProjectNotFoundError:
        # 404 — либо не существует, либо приватный без авторизации
        return JSONResponse(
            {"error": "Проект не найден. Если это приватный/draft модпак — "
                      "добавьте Modrinth PAT в /tokens (modrinth.com/settings/pats)."},
            status_code=404,
        )
    except UnauthorizedError:
        return JSONResponse(
            {"error": "Modrinth PAT невалиден или истёк. Обновите токен в /tokens."},
            status_code=401,
        )
    except ModrinthError as e:
        return JSONResponse(
            {"error": f"Modrinth API: {e}"},
            status_code=502,
        )

    project_id = info.get("id", project_id_or_slug)
    slug = info.get("slug") or ""
    title = info.get("title") or slug or project_id
    icon_url = info.get("icon_url") or ""
    project_type = info.get("project_type") or "modpack"

    # Опционально: получаем сразу последнюю версию и сохраняем её ID,
    # чтобы при первой проверке монитор НЕ считал её "новой" (не постил).
    try:
        latest = await modrinth.get_latest_version(project_id)
    except ModrinthError as e:
        _log.warning("get_latest_version failed for %s: %s", project_id, e)
        latest = None

    created = await db.g_add_modrinth_sub(
        guild_id, project_id, slug, title, icon_url, project_type, added_by
    )

    if latest:
        await db.g_update_modrinth_sub_last_version(
            guild_id, project_id=project_id,
            version_id=latest["id"],
            version_date=latest.get("date_published", ""),
        )

    return JSONResponse({
        "ok": True,
        "created": created,  # True если новая подписка, False если уже была (обновлён кеш)
        "project": {
            "id": project_id,
            "slug": slug,
            "title": title,
            "icon_url": icon_url,
            "project_type": project_type,
        },
        "latest_version": {
            "id": latest["id"],
            "version_number": latest.get("version_number"),
            "date_published": latest.get("date_published"),
        } if latest else None,
    })


@router.post("/api/modpacks/{project_id_or_slug}/remove")
async def api_remove_modpack(
    project_id_or_slug: str,
    _user: dict = Depends(require_admin),
):
    """Удалить подписку.

    Принимает как project_id (5FFgwNNP), так и slug (cobblemon-fabric).
    Сначала ищем по project_id, потом по slug.
    """
    guild_id = get_current_guild_id(_user)
    removed = await db.g_remove_modrinth_sub(guild_id, project_id_or_slug)
    if not removed:
        # Попробуем найти по slug и удалить по project_id
        sub = await db.g_find_modrinth_sub_by_slug(guild_id, project_id_or_slug)
        if sub:
            removed = await db.g_remove_modrinth_sub(guild_id, sub["project_id"])
    if not removed:
        return JSONResponse(
            {"error": "Подписка не найдена."},
            status_code=404,
        )
    return JSONResponse({"ok": True, "removed": True})


@router.post("/api/modpacks/check_now")
async def api_check_now(_user: dict = Depends(require_admin)):
    """Принудительно проверить все подписки на новые версии сейчас.

    Возвращает список найденных обновлений.
    Не постит в Discord — только проверяет (для UI).
    """
    guild_id = get_current_guild_id(_user)
    try:
        updates = await modrinth_monitor.check_once_for_guild(guild_id)
    except Exception as e:
        _log.error("check_now failed: %s", e, exc_info=True)
        return JSONResponse(
            {"error": f"Проверка не удалась: {e}"},
            status_code=500,
        )
    return JSONResponse({
        "ok": True,
        "updates": [
            {
                "project_id": u["project_id"],
                "title": u["project"].get("title", ""),
                "icon_url": u["project"].get("icon_url", ""),
                "version": {
                    "id": u["version"]["id"],
                    "version_number": u["version"].get("version_number"),
                    "name": u["version"].get("name"),
                    "date_published": u["version"].get("date_published"),
                    "version_type": u["version"].get("version_type"),
                    "changelog": (u["version"].get("changelog") or "")[:500],
                    "game_versions": u["version"].get("game_versions", []),
                    "loaders": u["version"].get("loaders", []),
                    "download_url": format_download_url(get_primary_file(u["version"]))
                        if get_primary_file(u["version"]) else None,
                    "filename": get_primary_file(u["version"]).get("filename")
                        if get_primary_file(u["version"]) else None,
                    "size": get_primary_file(u["version"]).get("size")
                        if get_primary_file(u["version"]) else None,
                },
                "is_first_check": u["is_first_check"],
            }
            for u in updates
        ],
    })


@router.post("/api/modpacks/save_channel")
async def api_save_channel(
    _user: dict = Depends(require_admin),
    channel_modpacks_id: str = Form(""),
):
    """Сохранить ID канала для постинга обновлений модпаков."""
    channel_modpacks_id = channel_modpacks_id.strip()
    if channel_modpacks_id and not channel_modpacks_id.isdigit():
        return JSONResponse(
            {"error": "ID канала должен быть числом."},
            status_code=400,
        )
    await db.set_setting("channel_modpacks_id", channel_modpacks_id, is_secret=False)
    return JSONResponse({"ok": True, "channel_modpacks_id": channel_modpacks_id})


@router.get("/api/modpacks/search")
async def api_search_modpacks(
    _user: dict = Depends(require_admin),
    q: str = "",
):
    """Поиск проектов на Modrinth (для подсказок в UI).

    Query: q — строка поиска (минимум 2 символа).
    Возвращает список до 10 проектов.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return JSONResponse({"projects": []})

    import httpx
    import json as _json
    headers = {"User-Agent": modrinth.USER_AGENT, "Accept": "application/json"}
    # Modrinth facets format: [["project_type:modpack"]] (array of arrays, OR inside, AND across)
    facets = _json.dumps([["project_type:modpack"]])
    params = {"query": q, "limit": "10", "facets": facets}
    try:
        async with httpx.AsyncClient(timeout=modrinth.REQUEST_TIMEOUT) as client:
            r = await client.get(
                f"{modrinth.MODRINTH_API}/search",
                headers=headers, params=params,
            )
        if r.status_code != 200:
            return JSONResponse(
                {"error": f"Modrinth search HTTP {r.status_code}"},
                status_code=502,
            )
        data = r.json()
    except httpx.RequestError as e:
        return JSONResponse(
            {"error": f"Network error: {e}"},
            status_code=502,
        )

    return JSONResponse({
        "projects": [
            {
                "project_id": p.get("project_id"),
                "slug": p.get("slug"),
                "title": p.get("title"),
                "description": (p.get("description") or "")[:200],
                "icon_url": p.get("icon_url"),
                "downloads": p.get("downloads", 0),
                "followers": p.get("followers", 0),
                "project_type": p.get("project_type"),
            }
            for p in data.get("hits", [])
        ]
    })
