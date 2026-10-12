"""FastAPI APIRouter — маршруты достижений (achievements).

Извлечено из web.py при рефакторинге (Task ID: refactor-ach-santa).
Содержит:
- HTML-страницу /achievements (админка)
- REST API /api/achievements/* (create, edit, delete, grant, revoke, users, my_grants,
  discord_roles, discord_roles/refresh, server_games, mark_read)
- REST API /api/profile/pin_achievements

Общие зависимости (auth, templates, anonymize_items) импортируются из deps.py —
без circular imports.

Вспомогательная функция _retro_grant_achievement живёт здесь же (раньше была
в web.py прямо перед блоком роутов ачивок) — она нужна только для
POST /api/achievements/create и не используется другими модулями.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

import crypto
import db
import ws_manager
from config import settings

from deps import (
    anonymize_items,
    get_current_guild_id,
    require_admin,
    require_superuser,
    require_user,
    templates,
)

router = APIRouter()


# === Helpers ===

async def _retro_grant_achievement(guild_id: int, ach_id: int, trigger_type: str, logger) -> None:
    """v2.1.0: Фоновая ретро-выдача ачивки. Перебирает только юзеров активных в этой
    гильдии (не всех глобальных юзеров) и проверяет каждого на соответствие триггеру.

    Не блокирует HTTP-ответ. Ошибки глушим и логируем — ачивка уже создана, ретро — best-effort.
    """
    try:
        user_ids = await db.list_guild_user_ids(guild_id)
        logger.info("Retro-grant ach#%s: checking %d active users in guild %s",
                    ach_id, len(user_ids), guild_id)
        retro_count = 0
        for user_did in user_ids:
            granted = await db.g_check_and_grant_auto(guild_id, user_did, trigger_type)
            if granted:
                retro_count += len(granted)
        if retro_count > 0:
            logger.info("Retro-grant ach#%s → %d users (out of %d checked)",
                        ach_id, retro_count, len(user_ids))
    except Exception as e:
        logger.warning("Retro-grant failed for ach#%s: %s", ach_id, e, exc_info=True)


# === Achievements: HTML-страница ===

@router.get("/achievements", response_class=HTMLResponse)
async def achievements_page(request: Request, _user: dict = Depends(require_admin)):
    """Страница управления ачивками. Только админ."""
    guild_id = get_current_guild_id(_user)
    achievements = await db.g_list_achievements(guild_id)
    # Для каждой ачивки — сколько юзеров её получили
    for ach in achievements:
        users = await db.g_get_users_with_achievement(guild_id, ach["id"])
        ach["granted_count"] = len([u for u in users if u["is_active"]])
    return templates.TemplateResponse(request, "achievements.html", {
        "user": _user,
        "achievements": achievements,
    })


# === Achievements: REST API ===

@router.post("/api/achievements/create")
async def api_create_achievement(
    _user: dict = Depends(require_admin),
    name: str = Form(...),
    description: str = Form(""),
    icon_key: str = Form(""),
    icon_url: str = Form(""),
    icon_color: str = Form(...),
    icon_glow: str = Form("none"),
    trigger_type: str = Form(...),
    trigger_threshold: int = Form(0),
    discord_role_id: str = Form(""),
    game_name: str = Form(""),
):
    """Создать ачивку.

    v2.0.4: если threshold пришёл пустым или не-числом, FastAPI сам возвращает 422.
    Фронт конвертирует threshold в секунды и использует String() чтобы избежать NaN.
    Здесь trigger_threshold уже int (FastAPI валидирует).
    """
    name = (name or "").strip()
    if not name or len(name) > 64:
        return JSONResponse({"error": "name required (1-64 chars)"}, status_code=400)
    if trigger_type not in db.ACHIEVEMENT_TRIGGERS:
        return JSONResponse(
            {"error": f"invalid trigger_type: {trigger_type}. Valid: {sorted(db.ACHIEVEMENT_TRIGGERS)}"},
            status_code=400,
        )
    # icon_url или icon_key — что-то одно должно быть
    icon_url = icon_url.strip() if icon_url else ""
    icon_key = icon_key.strip() if icon_key else ""
    if not icon_url and not icon_key:
        icon_key = "trophy"  # дефолт
    icon_config = {
        "icon_key": icon_key,
        "icon_url": icon_url,
        "color": icon_color,
        "glow": icon_glow,
        "game_name": game_name.strip() if game_name else "",
    }
    role_id = int(discord_role_id) if discord_role_id and discord_role_id.isdigit() else None
    guild_id = get_current_guild_id(_user)
    admin_id = _user.get("discord_id", 0)
    ach_logger = logging.getLogger("achievements")
    try:
        ach_id = await db.g_create_achievement(
            guild_id, name, description.strip() or None,
            icon_config, trigger_type, trigger_threshold,
            role_id, created_by=admin_id,
        )
    except ValueError as e:
        ach_logger.warning("Achievement create (ValueError): %s", e)
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        # v2.0.4: Любая другая ошибка (например, БД недоступна, нет таблицы, диск read-only)
        # — возвращаем понятный JSON с типом исключения и сообщением.
        # Раньше FastAPI возвращал голый 500 + HTML, фронт показывал «Ошибка создания».
        ach_logger.error("Achievement create failed: %s: %s", type(e).__name__, e, exc_info=True)
        return JSONResponse(
            {"error": f"{type(e).__name__}: {e}", "error_type": type(e).__name__},
            status_code=500,
        )

    # v2.1.0: C2 fix — ретроспективная выдача запускается в фоновой задаче, не блокируя
    # HTTP-ответ. Раньше итерировала ВСЕХ юзеров всех гильдий через list_users()
    # и блокировала запрос на 5+ минут → браузер отваливался по таймауту.
    # Теперь: только юзеры с активностью в текущей гильдии (list_guild_user_ids),
    # и в фоне (asyncio.create_task) — фронт сразу получает {"ok": true, "retro_pending": true}.
    if trigger_type != "manual":
        asyncio.create_task(_retro_grant_achievement(guild_id, ach_id, trigger_type, ach_logger))

    # v2.1.0: H4/H5/C3 fix — скачиваем иконку в /app/data/icons (персистентный путь,
    # переживает redeploy) и валидируем URL по схеме (http/https только) — иначе
    # админ мог передать javascript:/data: URL → XSS, или http://169.254.169.254/ → SSRF.
    if icon_url:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(icon_url)
            if parsed.scheme not in ("http", "https"):
                ach_logger.warning("Icon URL scheme rejected: %s", parsed.scheme)
                icon_url = ""
            elif not parsed.netloc:
                ach_logger.warning("Icon URL without host rejected: %s", icon_url)
                icon_url = ""
            else:
                import httpx
                import hashlib
                import os
                url_hash = hashlib.md5(icon_url.encode()).hexdigest()[:12]
                # v2.1.0: Персистентный путь в /app/data/icons (переживает redeploy Docker).
                # Раздаётся через symlink static/icons/custom → /app/data/icons,
                # или через fallback-роут /static/icons/custom/{filename}.
                data_dir = os.environ.get("DATABASE_PATH", "/app/data/bot.db")
                data_dir = os.path.dirname(os.path.abspath(data_dir)) if data_dir else "/app/data"
                icons_dir = os.path.join(data_dir, "icons")
                os.makedirs(icons_dir, exist_ok=True)
                local_filename = f"custom_{url_hash}.png"
                local_path = os.path.join(icons_dir, local_filename)
                # URL в БД указывает на fallback-роут (работает даже без symlink)
                icon_url_in_db = f"/static/icons/custom/{local_filename}"
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                    resp = await client.get(icon_url, headers={"User-Agent": "DeeBeelkin-IconFetcher/2.1"})
                    content_type = resp.headers.get("content-type", "").lower()
                    if resp.status_code == 200 and len(resp.content) > 100:
                        # Проверка что это реально изображение (по Content-Type или magic bytes)
                        if "image/" in content_type or resp.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"GIF8"):
                            with open(local_path, "wb") as f:
                                f.write(resp.content)
                            icon_config["icon_url"] = icon_url_in_db
                            await db.g_update_achievement_icon_config(guild_id, ach_id, icon_config)
                            ach_logger.info("Downloaded icon → %s", local_path)
                        else:
                            ach_logger.warning("Icon URL returned non-image content-type: %s", content_type)
                    else:
                        ach_logger.warning("Icon URL fetch failed: status=%s, size=%d", resp.status_code, len(resp.content))
        except Exception as e:
            ach_logger.warning("Icon download failed: %s", e)

    return JSONResponse({"ok": True, "id": ach_id, "retro_pending": trigger_type != "manual"})


@router.post("/api/achievements/{ach_id}/delete")
async def api_delete_achievement(
    ach_id: int,
    _user: dict = Depends(require_admin),
):
    """Удалить ачивку (каскадно — и все её выдачи)."""
    guild_id = get_current_guild_id(_user)
    deleted = await db.g_delete_achievement(guild_id, ach_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/achievements/{ach_id}/edit")
async def api_edit_achievement(
    ach_id: int,
    _user: dict = Depends(require_admin),
    name: str = Form(""),
    description: str = Form(""),
    trigger_type: str = Form(""),
    trigger_threshold: int = Form(-1),
    discord_role_id: str = Form(""),
    icon_color: str = Form(""),
    icon_glow: str = Form(""),
    icon_key: str = Form(""),
    icon_url: str = Form(""),
    game_name: str = Form(""),
):
    """Редактировать ачивку — название, описание, триггер, порог, роль, иконку."""
    guild_id = get_current_guild_id(_user)
    logger = logging.getLogger("achievements")
    name = name.strip()
    if not name or len(name) > 64:
        return JSONResponse({"error": "name required (1-64 chars)"}, status_code=400)

    # Собираем kwargs для g_update_achievement (только переданные поля)
    kwargs = {"name": name, "description": description}

    # trigger_type (пустая строка = не обновлять)
    if trigger_type:
        if trigger_type not in db.ACHIEVEMENT_TRIGGERS:
            return JSONResponse({"error": f"invalid trigger_type: {trigger_type}"}, status_code=400)
        kwargs["trigger_type"] = trigger_type

    # trigger_threshold (-1 = не обновлять, валидное значение = обновить)
    if trigger_threshold >= 0:
        kwargs["trigger_threshold"] = trigger_threshold

    # discord_role_id (пустая строка = не обновлять, "0" или число = обновить)
    if discord_role_id:
        if discord_role_id.isdigit():
            kwargs["discord_role_id"] = int(discord_role_id)
        elif discord_role_id == "none":
            kwargs["discord_role_id"] = 0  # убрать роль

    # icon_config (если хотя бы один параметр иконки передан — пересобираем config)
    if icon_color or icon_glow or icon_key or icon_url or game_name:
        # Читаем текущий config чтобы не потерять поля
        ach = await db.g_get_achievement(guild_id, ach_id)
        if not ach:
            return JSONResponse({"error": "not found"}, status_code=404)
        import json
        current_cfg = json.loads(ach["icon_config"]) if isinstance(ach["icon_config"], str) else (ach["icon_config"] or {})
        if icon_color:
            current_cfg["color"] = icon_color
        if icon_glow:
            current_cfg["glow"] = icon_glow
        if icon_key:
            current_cfg["icon_key"] = icon_key
        if icon_url:
            current_cfg["icon_url"] = icon_url
        if game_name:
            current_cfg["game_name"] = game_name.strip()
        kwargs["icon_config"] = current_cfg

    try:
        updated = await db.g_update_achievement(guild_id, ach_id, **kwargs)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("Edit achievement failed: %s: %s", type(e).__name__, e, exc_info=True)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if not updated:
        return JSONResponse({"error": "not found or no changes"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/achievements/{ach_id}/grant")
async def api_grant_achievement(
    ach_id: int,
    _user: dict = Depends(require_admin),
    user_discord_id: int = Form(...),
):
    """Вручную выдать ачивку юзеру. Если есть Discord роль — выдаёт и её."""
    guild_id = get_current_guild_id(_user)
    admin_id = _user.get("discord_id", 0)
    ach = await db.g_get_achievement(guild_id, ach_id)
    if not ach:
        return JSONResponse({"error": "achievement not found"}, status_code=404)
    ok = await db.g_grant_achievement(guild_id, ach_id, user_discord_id, granted_by=admin_id)
    if not ok:
        return JSONResponse({"error": "failed to grant"}, status_code=500)
    # Выдаём Discord роль если есть
    role_assigned = False
    if ach["discord_role_id"]:
        try:
            import bot as bot_module
            role_assigned = await bot_module.assign_role_to_member(int(guild_id), user_discord_id, int(ach["discord_role_id"]))
        except Exception as e:
            logging.getLogger("achievements").warning("Role assign failed: %s", e)
    return JSONResponse({"ok": True, "role_assigned": role_assigned})


@router.post("/api/achievements/{ach_id}/revoke")
async def api_revoke_achievement(
    ach_id: int,
    _user: dict = Depends(require_admin),
    user_discord_id: int = Form(...),
):
    """Отозвать ачивку. Снимает Discord роль если есть."""
    guild_id = get_current_guild_id(_user)
    ach = await db.g_get_achievement(guild_id, ach_id)
    if not ach:
        return JSONResponse({"error": "achievement not found"}, status_code=404)
    ok = await db.g_revoke_achievement(guild_id, ach_id, user_discord_id)
    if not ok:
        return JSONResponse({"error": "not granted or already revoked"}, status_code=404)
    # Снимаем Discord роль если есть
    if ach["discord_role_id"]:
        try:
            import bot as bot_module
            await bot_module.remove_role_from_member(int(guild_id), user_discord_id, int(ach["discord_role_id"]))
        except Exception as e:
            logging.getLogger("achievements").warning("Role remove failed: %s", e)
    return JSONResponse({"ok": True})


@router.get("/api/achievements/discord_roles")
async def api_get_discord_roles(_user: dict = Depends(require_admin)):
    """Получить список ролей Discord-сервера — из БД-кеша (быстро).

    v2.0.2: роли синхронизируются автоматически (on_ready + role events).
    Этот endpoint читает из БД — мгновенно, без запросов к Discord API.
    Для принудительного обновления кеша есть /api/achievements/discord_roles/refresh.

    v2.1.0: M20-22 fix — раньше требовал superuser, теперь require_admin.
    Админ (Трутень) может создавать ачивки, но не мог привязать роль — 403.
    """
    guild_id = get_current_guild_id(_user)
    roles = await db.g_list_discord_roles(guild_id)
    return JSONResponse({"roles": roles, "cached": True, "count": len(roles)})


@router.post("/api/achievements/discord_roles/refresh")
async def api_refresh_discord_roles(_user: dict = Depends(require_admin)):
    """Принудительно синхронизировать роли Discord-сервера с БД.

    Запрашивает актуальный список ролей у Discord и обновляет кеш.
    Используется кнопкой «🔄 Обновить роли» в конструкторе ачивок.
    """
    guild_id = get_current_guild_id(_user)
    try:
        import bot as bot_module
        count = await bot_module.sync_guild_roles_to_db(int(guild_id))
        # Заодно дедуплицируем игры при обновлении (умный housekeeping)
        deduped = await db.g_dedupe_games(guild_id)
        roles = await db.g_list_discord_roles(guild_id)
        return JSONResponse({
            "roles": roles,
            "cached": False,
            "count": len(roles),
            "synced": count,
            "deduped_games": deduped,
        })
    except Exception as e:
        return JSONResponse({"error": str(e), "roles": []}, status_code=500)


@router.get("/api/achievements/server_games")
async def api_get_server_games(_user: dict = Depends(require_admin)):
    """Список всех игр в которые играли на сервере (для триггеров ачивок).
    v2.3: использует merged список (Steam + Discord)."""
    guild_id = get_current_guild_id(_user)
    games = await db.g_list_server_games_merged(guild_id)
    return JSONResponse({"games": games})


@router.post("/api/profile/pin_achievements")
async def api_pin_achievements(
    _user: dict = Depends(require_user),
    achievement_ids: str = Form(""),
):
    """v2.3: Установить пинн-ачивки (до 3 штук) для показа под ником."""
    discord_id = _user.get("discord_id")
    if not discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)
    guild_id = get_current_guild_id(_user)
    # Парсим "1,2,3" → [1, 2, 3]
    try:
        ids = [int(x.strip()) for x in achievement_ids.split(",") if x.strip().isdigit()]
    except Exception:
        ids = []
    if len(ids) > 3:
        return JSONResponse({"error": "max 3 achievements"}, status_code=400)
    await db.g_set_pinned_achievements(guild_id, discord_id, ids)
    return JSONResponse({"ok": True, "pinned_count": len(ids)})


@router.post("/api/achievements/mark_read")
async def api_mark_achievements_read(_user: dict = Depends(require_user)):
    """Отметить что текущий юзер просмотрел свои ачивки — сбрасывает unread-бейдж.

    v2.0.3: вызывается сайдбаром при клике на бейдж колокольчика.
    """
    discord_id = _user.get("discord_id")
    if not discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)
    await db.g_mark_achievements_viewed(discord_id)
    return JSONResponse({"ok": True})


@router.get("/api/achievements/{ach_id}/users")
async def api_get_achievement_users(
    ach_id: int,
    _user: dict = Depends(require_admin),
):
    """Список юзеров, получивших конкретную ачивку."""
    guild_id = get_current_guild_id(_user)
    users = await db.g_get_users_with_achievement(guild_id, ach_id)
    return JSONResponse({"users": users, "count": len(users)})


@router.get("/api/achievements/my_grants")
async def api_get_my_grants(_user: dict = Depends(require_user)):
    """v2.3: Список ачивок текущего юзера — для выбора пинн-ачивок в /profile."""
    guild_id = get_current_guild_id(_user)
    discord_id = _user.get("discord_id")
    if not discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)
    achievements = await db.g_list_user_achievements(guild_id, discord_id, active_only=True)
    return JSONResponse({"achievements": achievements, "count": len(achievements)})
