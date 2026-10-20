"""FastAPI веб-панель.

- Логин по Discord ID (через проверку участия в сервере с ботом) или логин/пароль для админа
- Multi-tenant: каждый юзер видит данные своего current_guild_id
- Страницы: /, /wheel, /winners, /watched, /quotes, /profile, /select_guild
- Админ-only: /tokens, /channels, /features, /users, /guilds
- WebSocket на /ws/wheel для real-time обновлений колеса
- JSON API: /api/wheel/*, /api/quotes/*, /api/watchlist/*, /api/winners/*/rate
"""
from __future__ import annotations

import asyncio
import math
import os
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

import crypto
import db
import wheel_test_log
import ws_manager
from config import settings

# v2.3.3: модульная структура — wheel и movienight роуты в отдельных файлах
from deps import (
    serializer, templates, create_session, read_session,
    get_current_user, require_user, require_admin, require_superuser,
    get_current_guild_id, anonymize_items,
    _get_bot_version, _get_git_commit_id,
)
from timezone_utils import format_msk, format_msk_short
import wheel_routes
import movienight_routes

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 дней

app = FastAPI(title="Kinovecher Panel", docs_url=None, redoc_url=None, openapi_url=None)
# v2.3.3: templates импортирован из deps.py — настраиваем globals ОДИН раз
# bot_version и commit_id вычисляются через _get_git_commit_id() из deps.py
templates.env.globals['bot_version'] = _get_bot_version()
templates.env.globals['commit_id'] = _get_git_commit_id()
templates.env.globals['bot_version_with_commit'] = f"{_get_bot_version()}({_get_git_commit_id()})"


# v2.0.4: Глобальный handler для 422 — FastAPI по умолчанию возвращает {detail: [{loc, msg, type, ...}]}
# Фронт теперь умеет парсить этот формат, но также добавим человекочитаемое поле `error`.
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        loc = err.get("loc", [])
        field = loc[-1] if loc else "?"
        msg = err.get("msg", "")
        errors.append(f"{field}: {msg}")
    return JSONResponse(
        status_code=422,
        content={
            "error": "; ".join(errors) if errors else "validation failed",
            "detail": exc.errors(),  # оригинальный формат FastAPI для совместимости
        },
    )


# v3.0.3: Cache-Control middleware — HTML responses должны быть no-store,
# иначе после deploy браузер отдаёт закешированную старую страницу (с старым
# JS-кодом внутри, который использует устаревший guild_id / Discord ID).
# Это было причиной бага: /movienight показывал guild_id=0 потому что в
# кэше браузера лежал HTML с того времени, когда current_guild_id в сессии
# был 0. /wheel работал потому что пользователь зашёл впервые — HTML был свежий.
# Static files кэшируются через ?v=commit_id в URL (см. Jinja globals).
@app.middleware("http")
async def no_cache_html_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    except Exception:
        pass
    return response


_STATIC_DIR = Path(__file__).parent / "static"
_cached_bot_version: str | None = None

# v2.3.3: Lock против race condition при одновременных спинах.
# Ключ — guild_id. Не даёт двум юзерам одновременно крутить колесо в одной гильдии,
# что могло привести к удалению одного и того же item'а дважды и рассинхрону клиентов.

# v2.3.3 Вариант D: состояние активных спинов.
# spin_id → {guild_id, items, mode, is_test, duration, started_at, completed}
# Используется /api/wheel/result для валидации и fallback-таймером.
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# v2.1.0: C3 fix — custom_*.png иконки ачивок сохраняем в /app/data/icons
# (персистентный путь, переживает redeploy Docker-образа). Создаём symlink
# static/icons/custom → /app/data/icons чтобы StaticFiles их раздавал по /static/icons/.
# Symlink создаётся один раз при старте, не пересоздаётся если уже есть.
import os as _os
import logging as _logging
_data_icons_dir = _os.environ.get("DATABASE_PATH", "/app/data/bot.db")
_data_icons_dir = _os.path.dirname(_os.path.abspath(_data_icons_dir)) if _data_icons_dir else "/app/data"
_data_icons_dir = _os.path.join(_data_icons_dir, "icons")
try:
    _os.makedirs(_data_icons_dir, exist_ok=True)
    _static_icons_link = _STATIC_DIR / "icons" / "custom"
    if not _static_icons_link.exists() and (_STATIC_DIR / "icons").exists():
        try:
            _static_icons_link.symlink_to(_data_icons_dir, target_is_directory=True)
            _logging.getLogger("achievements").info("Created symlink %s → %s", _static_icons_link, _data_icons_dir)
        except (OSError, NotImplementedError):
            # Symlink не поддерживается (Windows без прав, или файл уже есть) — fallback:
            # будем раздавать напрямую через дополнительный route ниже.
            pass
except Exception as _e:
    _logging.getLogger("achievements").warning("Failed to setup /app/data/icons: %s", _e)


@app.get("/static/icons/custom/{filename:path}")
async def _serve_custom_icon(filename: str):
    """v2.1.0: Fallback-роут для раздачи custom_*.png из /app/data/icons,
    если symlink не сработал (Windows-хосты без прав)."""
    from fastapi.responses import FileResponse
    # Защита от path traversal
    if ".." in filename or "/" in filename:
        return JSONResponse({"error": "invalid filename"}, status_code=400)
    file_path = _os.path.join(_data_icons_dir, filename)
    if not _os.path.exists(file_path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(file_path)


# === Session ===
# create_session и read_session импортированы из deps.py


def _is_https_request(request: Request) -> bool:
    """Определить, идёт ли запрос по HTTPS.

    Учитывает X-Forwarded-Proto (если за reverse-proxy: nginx, BotHost, etc.).
    """
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
    return forwarded_proto == "https"


def _cookie_secure_flag(request: Request) -> dict:
    """Вернуть kwargs для set_cookie: secure=True только на HTTPS.

    На HTTP (dev) secure=True сделал бы cookie невидимым для браузера.
    """
    return {"secure": True} if _is_https_request(request) else {}

# read_session импортирована из deps.py


app.include_router(wheel_routes.router)
app.include_router(movienight_routes.router)
# v3.1: Village Quest integration — REST API + gateway endpoints for transport B
import vq_routes as _vq_routes
import vq_gateway as _vq_gateway
app.include_router(_vq_routes.router)
app.include_router(_vq_gateway.router)

@app.on_event("startup")
async def _startup():
    await db.init_db()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/ping/discord")
async def api_ping_discord(_user: dict = Depends(require_user)):
    """Проверка соединения с Discord (бот онлайн + latency)."""
    try:
        import bot as bot_module
        bot_instance = bot_module.get_bot_instance()
        if not bot_instance or not bot_instance.is_ready():
            return JSONResponse({"online": False, "error": "bot not ready"})
        latency_ms = round(bot_instance.latency * 1000, 1) if bot_instance.latency else None
        return JSONResponse({"online": True, "latency_ms": latency_ms})
    except Exception as e:
        return JSONResponse({"online": False, "error": str(e)})


@app.get("/api/ping/telegram")
async def api_ping_telegram(_user: dict = Depends(require_user)):
    """Проверка соединения с Telegram Bot API."""
    import httpx
    raw_token = await db.get_setting("telegram_token")
    if not raw_token:
        return JSONResponse({"online": False, "error": "token not set"})
    token = crypto.decrypt(raw_token)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            if r.status_code == 200:
                data = r.json()
                bot_info = data.get("result", {})
                return JSONResponse({
                    "online": True,
                    "bot_name": bot_info.get("username", ""),
                })
            return JSONResponse({"online": False, "error": f"HTTP {r.status_code}"})
    except Exception as e:
        return JSONResponse({"online": False, "error": str(e)})


@app.post("/theme")
async def set_theme(request: Request, theme: str = Form(...)):
    """Установить тему (light/dark). Записывает куку и редиректит обратно."""
    if theme not in ("light", "dark"):
        theme = "light"
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax", **_cookie_secure_flag(request))
    return resp


def _theme(request: Request) -> str:
    """Текущая тема из куки (light по умолчанию)."""
    return request.cookies.get("theme", "light")


# Регистрируем глобально для всех шаблонов, чтобы не передавать явно
from timezone_utils import format_msk, format_msk_short
templates.env.globals["theme_from_request"] = lambda request: request.cookies.get("theme", "light")
templates.env.globals["format_msk"] = format_msk
templates.env.globals["format_msk_short"] = format_msk_short


# v1.8.0: проверка включён ли модуль Тайный Санта
async def is_santa_enabled_async() -> bool:
    """Проверяет включён ли модуль Тайный Санта (тумблер в /features)."""
    val = await db.get_setting("santa_enabled")
    return val == "1"


def is_santa_enabled_sync() -> bool:
    """Sync-версия для Jinja2 шаблонов (читает из кеша db._settings_cache)."""
    import time
    cached = db._settings_cache.get("santa_enabled")
    if cached and time.time() - cached[1] < db._CACHE_TTL:
        return cached[0] == "1"
    return False


# Алиас для удобства
async def is_santa_enabled() -> bool:
    return await is_santa_enabled_async()


templates.env.globals["santa_enabled"] = is_santa_enabled_sync


async def get_user_guilds(user_payload: dict) -> list[dict]:
    """Список серверов, доступных пользователю.
    - Обычный юзер: только те сервера, где он участник (через бота) и которые approved
    - Админ: ВСЕ сервера (даже не approved, даже не где он участник)
    """
    import guild as guild_module
    if user_payload.get("is_admin"):
        return await guild_module.list_guilds(approved_only=False)
    # Обычный юзер — через бота проверяем где он участник
    import bot as bot_module
    discord_id = user_payload.get("discord_id", 0)
    if not discord_id:
        return []
    all_guilds = await guild_module.list_guilds(approved_only=True)
    result = []
    for g in all_guilds:
        # Проверяем через бота
        is_member, _ = await bot_module.is_guild_member(discord_id)
        # is_guild_member ищет по всем серверам бота — упрощённая проверка
        # TODO: в Фазе 3 сделаем per-guild проверку
        if is_member:
            result.append(g)
    return result


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "login.html", {
        "error": error,
    })


@app.post("/login")
async def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form("", alias="password"),
):
    """Двухшаговый логин:
    Шаг 1: юзер вводит Discord ID → бот проверяет участника сервера
    Шаг 2: если у юзера нет пароля → редирект на /set-password
           если есть пароль → проверка пароля → сессия
    Админ может войти через env ADMIN_PASSWORD (если совпадает).
    """
    login = login.strip()
    password = password.strip()

    is_env_admin_pass = settings.admin_password and secrets.compare_digest(password, settings.admin_password)

    # Способ 1: классический env-админ по логину+паролю (login="admin", password="changeme")
    is_env_admin_login = secrets.compare_digest(login, settings.admin_login)
    if is_env_admin_login and is_env_admin_pass and not login.isdigit():
        admin_guild_id = 0
        if settings.admin_discord_id:
            try:
                import bot as bot_module
                is_member, member_info = await bot_module.is_guild_member(settings.admin_discord_id)
                if is_member and member_info:
                    admin_guild_id = member_info.get("guild_id", 0)
                    import guild as guild_module
                    await guild_module.init_guild_tables(admin_guild_id)
                    await guild_module.upsert_guild(
                        admin_guild_id,
                        member_info.get("guild_name", "Discord Server"),
                        auto_approve=True,
                    )
            except Exception:
                pass
        user_payload = {
            "discord_id": 0,
            "username": login,
            "is_admin": True,
            "is_superuser": True,
            "role": "superuser",
            "avatar_url": None,
            "current_guild_id": admin_guild_id,
        }
        token = create_session(user_payload)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", token, max_age=SESSION_TTL, httponly=True, samesite="lax", **_cookie_secure_flag(request))
        return resp

    # Способ 2: вход по Discord ID (число)
    if login.isdigit():
        discord_id = int(login)

        # Проверка участника сервера через бота
        import bot as bot_module
        is_member, member_info = await bot_module.is_guild_member(discord_id)
        if not is_member:
            return RedirectResponse(url="/login?error=not_member", status_code=303)

        # Данные из Discord
        display_name = member_info["display_name"]
        username = member_info["username"]
        avatar_url = member_info.get("avatar_url")
        roles = member_info.get("roles", [])
        top_role = member_info.get("top_role")
        guild_name = member_info.get("guild_name")
        member_guild_id = member_info.get("guild_id", 0)

        # Проверка прав
        is_admin = await db.is_admin(discord_id)
        is_superuser = await db.is_superuser(discord_id)
        user_role = await db.get_user_role(discord_id)

        # Если это Discord ID админа (env ADMIN_DISCORD_ID) — проверяем env ADMIN_PASSWORD
        if settings.admin_discord_id is not None and discord_id == settings.admin_discord_id:
            if not is_env_admin_pass:
                return RedirectResponse(url="/login?error=admin_password", status_code=303)
            is_admin = True
            is_superuser = True
            user_role = "superuser"

        # Авто-создание/обновление юзера в БД
        await db.upsert_user(
            discord_id,
            username=username,
            display_name=display_name,
            avatar_url=avatar_url,
            roles=roles,
            top_role=top_role,
            guild_name=guild_name,
        )
        if is_admin:
            await db.set_admin(discord_id, True)

        # Регистрируем guild
        if member_guild_id:
            import guild as guild_module
            try:
                await guild_module.init_guild_tables(member_guild_id)
                if is_admin:
                    await guild_module.upsert_guild(
                        member_guild_id, guild_name or "Discord Server",
                        auto_approve=True,
                    )
                else:
                    await guild_module.upsert_guild(
                        member_guild_id, guild_name or "Discord Server",
                    )
            except Exception as e:
                import logging
                logging.getLogger("web").warning("Failed to init guild %s: %s", member_guild_id, e)

        # === Двухшаговый логин: проверяем пароль ===
        # Админ с env ADMIN_PASSWORD — пропускает проверку пароля БД
        if not (settings.admin_discord_id is not None and discord_id == settings.admin_discord_id):
            user_has_password = await db.has_password(discord_id)
            if not user_has_password:
                # Нет пароля → возвращаем на логин с попапом установки пароля
                # Сохраняем discord_id в временной сессии (через куку)
                resp = RedirectResponse(url=f"/login?need_password=1&did={discord_id}", status_code=303)
                # Временная кука на 5 минут для установки пароля
                temp_token = create_session({
                    "discord_id": discord_id,
                    "username": display_name,
                    "is_admin": is_admin,
                    "avatar_url": avatar_url,
                    "current_guild_id": member_guild_id,
                    "temp": True,
                })
                resp.set_cookie("temp_session", temp_token, max_age=300, httponly=True, samesite="lax", **_cookie_secure_flag(request))
                return resp

            # Есть пароль → проверяем
            if not password:
                # Пароль не введён — возвращаем на логин с подсказкой
                return RedirectResponse(url=f"/login?error=password_required&did={discord_id}", status_code=303)

            if not await db.verify_user_password(discord_id, password):
                return RedirectResponse(url=f"/login?error=wrong_password&did={discord_id}", status_code=303)

        # Успешный вход
        user_payload = {
            "discord_id": discord_id,
            "username": display_name,
            "is_admin": is_admin,
            "is_superuser": is_superuser,
            "role": user_role,
            "avatar_url": avatar_url,
            "roles": roles,
            "top_role": top_role,
            "guild_name": guild_name,
            "current_guild_id": member_guild_id,
        }
        token = create_session(user_payload)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", token, max_age=SESSION_TTL, httponly=True, samesite="lax", **_cookie_secure_flag(request))
        resp.delete_cookie("temp_session")
        return resp

    return RedirectResponse(url="/login?error=invalid", status_code=303)


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session")
    resp.delete_cookie("temp_session")
    return resp


# === Set Password (v1.8.2) ===

@app.get("/set-password", response_class=HTMLResponse)
async def set_password_page(request: Request, discord_id: Optional[int] = None):
    """Страница установки пароля. Доступ через temp_session куку."""
    # Читаем temp_session
    temp_token = request.cookies.get("temp_session")
    if not temp_token:
        return RedirectResponse(url="/login", status_code=303)
    temp_user = read_session(temp_token)
    if not temp_user or not temp_user.get("temp"):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "set_password.html", {
        "discord_id": temp_user.get("discord_id", 0),
        "username": temp_user.get("username", ""),
    })


@app.post("/set-password")
async def set_password_submit(
    request: Request,
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    """Установить пароль для юзера."""
    temp_token = request.cookies.get("temp_session")
    if not temp_token:
        return RedirectResponse(url="/login", status_code=303)
    temp_user = read_session(temp_token)
    if not temp_user or not temp_user.get("temp"):
        return RedirectResponse(url="/login", status_code=303)

    discord_id = temp_user.get("discord_id", 0)
    if not discord_id:
        return RedirectResponse(url="/login?error=invalid", status_code=303)

    password = password.strip()
    password_confirm = password_confirm.strip()

    if not password:
        return RedirectResponse(url="/set-password?error=empty", status_code=303)
    if len(password) < 8:
        return RedirectResponse(url="/set-password?error=short", status_code=303)
    if password != password_confirm:
        return RedirectResponse(url="/set-password?error=mismatch", status_code=303)

    # Устанавливаем пароль
    success = await db.set_user_password(discord_id, password)
    if not success:
        return RedirectResponse(url="/set-password?error=failed", status_code=303)

    # Возвращаем на логин — юзер должен войти с новым паролем
    resp = RedirectResponse(url=f"/login?error=password_set&did={discord_id}", status_code=303)
    resp.delete_cookie("temp_session")
    return resp


@app.post("/api/users/{discord_id}/reset-password")
async def api_reset_password(
    discord_id: int,
    _user: dict = Depends(require_superuser),
):
    """Сбросить пароль юзера (только админ).
    Юзер получит DM в Discord с уведомлением.
    """
    success = await db.reset_user_password(discord_id)
    if not success:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Отправляем DM юзеру в Discord
    try:
        import bot as bot_module
        bot_instance = bot_module.get_bot_instance()
        if bot_instance:
            member = None
            for g in bot_instance.guilds:
                member = g.get_member(discord_id)
                if member is None:
                    try:
                        member = await g.fetch_member(discord_id)
                    except Exception:
                        member = None
                if member:
                    break
            if member:
                try:
                    await member.send(
                        "ℹ️ Ваш пароль от веб-панели был сброшен администратором.\n"
                        "При следующем входе вам будет предложено установить новый пароль."
                    )
                except Exception as e:
                    import logging
                    logging.getLogger("web").warning("Failed to DM user %s: %s", discord_id, e)
    except Exception as e:
        import logging
        logging.getLogger("web").warning("Reset password DM failed: %s", e)

    return JSONResponse({"ok": True, "discord_id": discord_id})


@app.get("/select_guild", response_class=HTMLResponse)
async def select_guild_page(request: Request, _user: dict = Depends(require_user)):
    """Страница выбора сервера."""
    guilds = await get_user_guilds(_user)
    current_guild_id = get_current_guild_id(_user)
    return templates.TemplateResponse(request, "select_guild.html", {
        "user": _user,
        "guilds": guilds,
        "current_guild_id": current_guild_id,
    })


@app.post("/select_guild")
async def select_guild_submit(
    request: Request,
    _user: dict = Depends(require_user),
    guild_id: int = Form(...),
):
    """Переключиться на выбранный сервер. Обновляет current_guild_id в сессии."""
    # Проверяем что юзер имеет доступ к этому серверу
    available = await get_user_guilds(_user)
    available_ids = {g["guild_id"] for g in available}
    if guild_id not in available_ids:
        return RedirectResponse(url="/select_guild?error=no_access", status_code=303)

    # Обновляем сессию с новым current_guild_id
    _user["current_guild_id"] = guild_id
    token = create_session(_user)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("session", token, max_age=SESSION_TTL, httponly=True, samesite="lax", **_cookie_secure_flag(request))
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _user: dict = Depends(require_user)):
    guild_id = get_current_guild_id(_user)

    # Статусы токенов (глобальные)
    saved_kp = bool(await db.get_setting("kinopoisk_token"))
    saved_tg = bool(await db.get_setting("telegram_token"))

    # Версия бота для footer (кешированная — не парсит CHANGELOG при каждом запросе)
    global _cached_bot_version
    if not _cached_bot_version:
        from changelog_parser import get_latest_version
        _cached_bot_version = get_latest_version("CHANGELOG.md")
    bot_version = _cached_bot_version

    # init_guild_tables НЕ вызываем — таблицы уже созданы при on_ready бота.
    # Дополнительный вызов при каждом запросе = спам в логах + лишний I/O.

    # === «Сейчас» — статус киновечера ===
    active_collection = await db.g_get_active_collection(guild_id)
    ready_count = 0
    total_count = 0
    my_collection_status = None
    if active_collection:
        participants = await db.g_list_collection_participants(guild_id, active_collection["id"])
        total_count = len(participants)
        ready_count = sum(1 for p in participants if p["is_ready"])
        user_discord_id = _user.get("discord_id", 0)
        my_p = await db.g_get_santa_participant(guild_id, active_collection["id"], user_discord_id) if False else None
        # Проверяем статус юзера в сборе
        for p in participants:
            if p["user_discord_id"] == user_discord_id:
                if p["kicked_at"]:
                    my_collection_status = "кикнут"
                elif p["is_ready"]:
                    my_collection_status = "готов"
                else:
                    my_collection_status = "ещё не готов"
                break
        if not my_collection_status:
            my_collection_status = "не присоединился"

    # Колесо
    wheel_items = await db.g_list_wheel_items(guild_id, active_only=True)
    wheel_count = len(wheel_items)

    # Недавний победитель
    recent_winner = await db.g_get_recent_winner(guild_id, since_minutes=60)

    # === Чек-лист настроек ===
    user_discord_id = _user.get("discord_id", 0)
    is_tg_linked = await db.is_tg_linked(user_discord_id) if user_discord_id else False
    is_steam_set = await db.is_steam_profile_set(user_discord_id) if user_discord_id else False
    tg_link = await db.get_tg_link(user_discord_id) if user_discord_id else None
    tg_username = tg_link[2] if tg_link and tg_link[2] else ""

    # === Админ данные ===
    admin_activity = []
    unrated_winners = []
    stats = {}
    if _user.get("is_admin"):
        # Активность за 7 дней
        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()

        # Последние входы юзеров
        async with db._connect() as conn:
            async with conn.execute(
                "SELECT discord_id, username, display_name, last_login_at FROM users WHERE last_login_at > ? ORDER BY last_login_at DESC LIMIT 10",
                (cutoff,)
            ) as cur:
                for r in await cur.fetchall():
                    user_did = r[0]
                    username = r[2] or r[1] or "Unknown"
                    login_at = r[3]
                    # Вычисляем "time ago"
                    if login_at:
                        try:
                            dt = datetime.fromisoformat(login_at)
                            diff = datetime.utcnow() - dt
                            if diff.days > 0:
                                time_ago = f"{diff.days} дн назад"
                            elif diff.seconds > 3600:
                                time_ago = f"{diff.seconds // 3600} ч назад"
                            else:
                                time_ago = f"{diff.seconds // 60} мин назад"
                        except Exception:
                            time_ago = "недавно"
                    else:
                        time_ago = "недавно"
                    admin_activity.append({
                        "discord_id": user_did,
                        "username": username,
                        "action": "заходил(а) в панель",
                        "time_ago": time_ago,
                    })

        # Неоценённые победители
        all_winners = await db.g_get_winners_with_ratings(guild_id, limit=20)
        for w in all_winners:
            if w["ratings_count"] < 3 and len(unrated_winners) < 5:
                unrated_winners.append({
                    "lot_name": w["lot_name"],
                    "ratings_count": w["ratings_count"],
                    "total_count": 4,  # примерный максимум участников
                })

        # Статистика сервера
        all_users = await db.list_users()
        all_watched = await db.g_list_watched(guild_id, limit=10000)
        all_winners_count = len(all_winners)
        all_quotes = await db.g_count_quotes(guild_id)
        total_ratings = 0
        for w in all_winners:
            total_ratings += w["ratings_count"]
        # Активные за неделю
        active_week = len(admin_activity)

        stats = {
            "total_users": len(all_users),
            "total_watched": len(all_watched),
            "total_ratings": total_ratings,
            "total_quotes": all_quotes,
            "total_winners": all_winners_count,
            "active_week": active_week,
        }

    # Активные участники (виден всем юзерам, не только админ)
    # v2.0.1: берём 22, enriched Discord статусами, сортировка online→idle→dnd→offline
    active_members = await db.list_active_members_with_discord(limit=22)
    # Обогащаем Discord статусами
    try:
        import bot as bot_module
        status_order = {'online': 0, 'idle': 1, 'dnd': 2, 'offline': 3}
        for m in active_members:
            try:
                info = await bot_module.get_member_discord_info(m["discord_id"], int(guild_id))
                if info:
                    m["status"] = info["status"]
                    m["status_emoji"] = info["status_emoji"]
                    m["current_game"] = info["current_game"]
                    m["voice_channel"] = info["voice_channel"]
            except Exception:
                pass
        # Сортировка: online → idle → dnd → offline
        active_members.sort(key=lambda m: status_order.get(m.get("status", "offline"), 3))
        active_members = active_members[:22]  # максимум 22
    except Exception:
        pass

    # v2.3.2: Совместные игровые сессии (только если бот запущен)
    joint_sessions = []
    try:
        joint_sessions = await bot_module.get_joint_play_sessions(int(guild_id))
    except Exception as e:
        import logging
        logging.getLogger("dashboard").warning("joint_play_sessions failed: %s", e)

    return templates.TemplateResponse(request, "panel.html", {
        "user": _user,
        "current_guild_id": guild_id,
        "bot_version": bot_version,
        "saved_kp": saved_kp,
        "saved_tg": saved_tg,
        "active_collection": active_collection,
        "ready_count": ready_count,
        "total_count": total_count,
        "my_collection_status": my_collection_status,
        "wheel_count": wheel_count,
        "recent_winner": recent_winner,
        "is_tg_linked": is_tg_linked,
        "is_steam_set": is_steam_set,
        "tg_username": tg_username,
        "admin_activity": admin_activity,
        "unrated_winners": unrated_winners,
        "stats": stats,
        "active_members": active_members,
        "joint_sessions": joint_sessions,
    })


# === Tokens (admin only) ===

KNOWN_TOKENS = [
    ("discord_token", "Discord Bot Token"),
    ("telegram_token", "Telegram Bot Token"),
    ("kinopoisk_token", "Kinopoisk API Token (kinopoiskapiunofficial.tech)"),
    ("steam_api_key", "Steam Web API Key (steamcommunity.com/dev/apikey) — для модуля Тайный Санта"),
    ("github_token", "GitHub Personal Access Token (опционально) — для мониторинга версий. Без него лимит 60 req/hour"),
    ("modrinth_token", "Modrinth Personal Access Token (опционально) — для подписок на приватные/draft модпаки. Создаётся в modrinth.com/settings/pats"),
]


@app.get("/tokens", response_class=HTMLResponse)
async def tokens_form(request: Request, _user: dict = Depends(require_superuser)):
    saved = {}
    for key, _label in KNOWN_TOKENS:
        v = await db.get_setting(key)
        saved[key] = bool(v)
    return templates.TemplateResponse(request, "tokens.html", {
        "user": _user,
        "tokens": KNOWN_TOKENS,
        "saved": saved,
    })


@app.post("/tokens")
async def tokens_save(
    request: Request,
    _user: dict = Depends(require_superuser),
    discord_token: str = Form(""),
    telegram_token: str = Form(""),
    kinopoisk_token: str = Form(""),
    steam_api_key: str = Form(""),
    github_token: str = Form(""),
    modrinth_token: str = Form(""),
):
    updates = {
        "discord_token": discord_token.strip(),
        "telegram_token": telegram_token.strip(),
        "kinopoisk_token": kinopoisk_token.strip(),
        "steam_api_key": steam_api_key.strip(),
        "github_token": github_token.strip(),
        "modrinth_token": modrinth_token.strip(),
    }
    for key, val in updates.items():
        if val:
            encrypted = crypto.encrypt(val)
            await db.set_setting(key, encrypted, is_secret=True)
    return RedirectResponse(url="/tokens?saved=1", status_code=303)


# === Channels (admin only) ===

KNOWN_CHANNELS = [
    ("panel_base_url", "URL веб-панели (для кнопок в Telegram, например https://your-bot.bothost.tech)"),
    ("channel_quotes_id", "ID канала #цитатник"),
    ("channel_announce_id", "ID канала анонсов киновечера"),
    ("channel_winners_id", "ID канала #winners (куда постить победителей колеса)"),
    ("channel_updates_id", "ID канала обновлений бота (куда постить changelog)"),
    ("channel_admin_updates_id", "ID канала для админских анонсов (опционально; если пусто — шлём в DM админу)"),
    ("role_movie_ping_id", "ID роли для пинга анонсов"),
    ("telegram_chat_id", "Telegram chat_id для бэклога просмотренных"),
    ("telegram_thread_id", "Telegram thread_id (ID темы форума, опционально)"),
    ("tg_winners_chat_id", "Telegram chat_id для победителей колеса (опционально)"),
    ("tg_winners_thread_id", "Telegram thread_id для победителей колеса (опционально)"),
]


@app.get("/channels", response_class=HTMLResponse)
async def channels_form(request: Request, _user: dict = Depends(require_superuser)):
    values = {key: (await db.get_setting(key) or "") for key, _label in KNOWN_CHANNELS}
    return templates.TemplateResponse(request, "channels.html", {
        "user": _user,
        "channels": KNOWN_CHANNELS,
        "values": values,
    })


@app.post("/channels")
async def channels_save(
    request: Request,
    _user: dict = Depends(require_superuser),
    panel_base_url: str = Form(""),
    channel_quotes_id: str = Form(""),
    channel_announce_id: str = Form(""),
    channel_winners_id: str = Form(""),
    channel_updates_id: str = Form(""),
    channel_admin_updates_id: str = Form(""),
    role_movie_ping_id: str = Form(""),
    telegram_chat_id: str = Form(""),
    telegram_thread_id: str = Form(""),
    tg_winners_chat_id: str = Form(""),
    tg_winners_thread_id: str = Form(""),
):
    updates = {
        "panel_base_url": panel_base_url.strip(),
        "channel_quotes_id": channel_quotes_id.strip(),
        "channel_announce_id": channel_announce_id.strip(),
        "channel_winners_id": channel_winners_id.strip(),
        "channel_updates_id": channel_updates_id.strip(),
        "channel_admin_updates_id": channel_admin_updates_id.strip(),
        "role_movie_ping_id": role_movie_ping_id.strip(),
        "telegram_chat_id": telegram_chat_id.strip(),
        "telegram_thread_id": telegram_thread_id.strip(),
        "tg_winners_chat_id": tg_winners_chat_id.strip(),
        "tg_winners_thread_id": tg_winners_thread_id.strip(),
    }
    for key, val in updates.items():
        if val:
            await db.set_setting(key, val, is_secret=False)
        else:
            await db.delete_setting(key)
    return RedirectResponse(url="/channels?saved=1", status_code=303)


# === Features toggles ===

KNOWN_FEATURES = [
    ("tg_crosspost_quotes", "Кросс-постить цитаты в Telegram", "0"),
    ("tg_crosspost_announce", "Кросс-постить анонсы киновечера в Telegram", "0"),
    ("santa_enabled", "Включить модуль «Тайный Санта» (Steam-игры)", "0"),
    ("db_backup_enabled", "Еженедельный бэкап БД в Telegram (для админа)", "1"),
]


@app.get("/features", response_class=HTMLResponse)
async def features_form(request: Request, _user: dict = Depends(require_superuser)):
    values = {key: (await db.get_setting(key) or default) for key, _label, default in KNOWN_FEATURES}
    return templates.TemplateResponse(request, "features.html", {
        "user": _user,
        "features": KNOWN_FEATURES,
        "values": values,
        "is_admin": _user.get("is_admin", False),
    })


@app.post("/features")
async def features_save(
    request: Request,
    _user: dict = Depends(require_superuser),
    tg_crosspost_quotes: bool = Form(False),
    tg_crosspost_announce: bool = Form(False),
    santa_enabled: bool = Form(False),
    db_backup_enabled: bool = Form(False),
):
    await db.set_setting("tg_crosspost_quotes", "1" if tg_crosspost_quotes else "0", is_secret=False)
    await db.set_setting("tg_crosspost_announce", "1" if tg_crosspost_announce else "0", is_secret=False)
    await db.set_setting("santa_enabled", "1" if santa_enabled else "0", is_secret=False)
    await db.set_setting("db_backup_enabled", "1" if db_backup_enabled else "0", is_secret=False)
    return RedirectResponse(url="/features?saved=1", status_code=303)


# === Admin panel (v2.3.1 — только для Матка) ===

@app.get("/adminpanel", response_class=HTMLResponse)
async def admin_panel_page(request: Request, _user: dict = Depends(require_superuser)):
    """Единая админ-панель с табами: Юзеры, Токены, Каналы, Фичи, Серверы."""
    guild_id = get_current_guild_id(_user)
    # Данные для вкладки Юзеры
    users = await db.list_users()
    # Данные для вкладки Токены
    saved_tokens = {}
    for key, _label in KNOWN_TOKENS:
        v = await db.get_setting(key)
        saved_tokens[key] = bool(v)
    # Данные для вкладки Каналы
    channel_values = {key: (await db.get_setting(key) or "") for key, _label in KNOWN_CHANNELS}
    # Данные для вкладки Фичи
    feature_values = {key: (await db.get_setting(key) or default) for key, _label, default in KNOWN_FEATURES}
    # Данные для вкладки Серверы
    import guild as guild_module
    guilds = await guild_module.list_guilds(approved_only=False)

    return templates.TemplateResponse(request, "admin_panel.html", {
        "user": _user,
        "users": users,
        "tokens": KNOWN_TOKENS,
        "saved_tokens": saved_tokens,
        "channels": KNOWN_CHANNELS,
        "channel_values": channel_values,
        "features": KNOWN_FEATURES,
        "feature_values": feature_values,
        "guilds": guilds,
        "role_labels": db.ROLE_LABELS,
        "active_tab": request.query_params.get("tab", "users"),
    })


@app.post("/api/admin/add_user")
async def api_add_user(
    _user: dict = Depends(require_superuser),
    discord_id: str = Form(...),
    username: str = Form(""),
):
    """Добавить юзера вручную по Discord ID."""
    discord_id = discord_id.strip()
    if not discord_id.isdigit():
        return JSONResponse({"error": "Discord ID должен быть числом"}, status_code=400)
    discord_id = int(discord_id)
    # Проверяем не существует ли уже
    existing = await db.get_user(discord_id)
    if existing:
        return JSONResponse({"error": "Юзер уже существует"}, status_code=409)
    await db.upsert_user(discord_id, username=username or f"User {discord_id}", display_name=username or None)
    return JSONResponse({"ok": True})


@app.post("/api/admin/delete_user")
async def api_delete_user(
    _user: dict = Depends(require_superuser),
    discord_id: int = Form(...),
):
    """Удалить юзера из БД."""
    # Нельзя удалить env-админа
    if settings.admin_discord_id and discord_id == int(settings.admin_discord_id):
        return JSONResponse({"error": "Нельзя удалить env-админа"}, status_code=400)
    async with db._connect() as conn:
        await conn.execute("DELETE FROM users WHERE discord_id = ?", (discord_id,))
        await conn.commit()
    return JSONResponse({"ok": True})


@app.post("/api/admin/set_steam")
async def api_admin_set_steam(
    _user: dict = Depends(require_superuser),
    discord_id: int = Form(...),
    steam_url: str = Form(...),
):
    """Установить Steam-профиль юзеру через ссылку."""
    import steam
    steam_url = steam_url.strip()
    if not steam_url:
        # Очищаем
        await db.set_user_steam_profile(discord_id, None, None, None, None)
        return JSONResponse({"ok": True, "cleared": True})
    validation = await steam.validate_steam_profile(steam_url)
    if not validation["valid"]:
        return JSONResponse({"error": validation.get("error", "Невалидный профиль")}, status_code=400)
    await db.set_user_steam_profile(
        discord_id,
        validation["steam_profile_url"],
        validation["steam_id64"],
        validation["steam_persona"],
        validation["steam_avatar_url"],
    )
    return JSONResponse({"ok": True, "persona": validation.get("steam_persona", "")})


@app.post("/api/admin/set_role")
async def api_admin_set_role(
    _user: dict = Depends(require_superuser),
    discord_id: int = Form(...),
    role: str = Form(...),
):
    """Изменить роль юзера."""
    if role not in ("superuser", "junior-admin", "user"):
        return JSONResponse({"error": "Неверная роль"}, status_code=400)
    # Нельзя понизить env-админа
    if settings.admin_discord_id and discord_id == int(settings.admin_discord_id) and role != "superuser":
        return JSONResponse({"error": "Нельзя понизить env-админа"}, status_code=400)
    await db.set_user_role(discord_id, role)
    return JSONResponse({"ok": True})


@app.get("/api/profile/steam_status")
async def api_steam_status(_user: dict = Depends(require_superuser), discord_id: int = 0):
    """Проверить привязан ли Steam-профиль к юзеру (для админ-панели)."""
    if not discord_id:
        return JSONResponse({"linked": False})
    profile = await db.get_user_steam_profile(discord_id)
    if profile and profile.get("steam_id64"):
        return JSONResponse({"linked": True, "persona": profile.get("steam_persona", "")})
    return JSONResponse({"linked": False})


# === Winners page (для всех залогиненных) ===

@app.get("/winners", response_class=HTMLResponse)
async def winners_page(
    request: Request,
    _user: dict = Depends(require_user),
    q: str = "",
    page: int = 1,
):
    """Победители с агрегированными рейтингами + пагинация + поиск."""
    guild_id = get_current_guild_id(_user)
    page = max(1, page)
    per_page = 20
    offset = (page - 1) * per_page
    search = q.strip() or None
    winners = await db.g_get_winners_with_ratings(guild_id, limit=per_page, offset=offset, search=search)
    total = await db.g_count_winners(guild_id, search)
    user_discord_id = _user.get("discord_id", 0)
    for w in winners:
        w["user_rating"] = await db.g_get_user_rating(guild_id, w["id"], user_discord_id) if user_discord_id else None
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(request, "winners.html", {
        "user": _user,
        "winners": winners,
        "is_admin": _user.get("is_admin", False),
        "search": q,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    })


@app.post("/winners/{winner_id}/delete")
async def delete_winner_endpoint(
    winner_id: int,
    _user: dict = Depends(require_admin),
):
    """Удалить запись победителя (только админ)."""
    guild_id = get_current_guild_id(_user)
    deleted = await db.g_delete_winner(guild_id, winner_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return RedirectResponse(url="/winners?deleted=1", status_code=303)


@app.post("/api/winners/{winner_id}/rate")
async def api_rate_winner(
    winner_id: int,
    _user: dict = Depends(require_user),
    rating: float = Form(...),
    review: str = Form(""),
):
    """Поставить или обновить оценку победителю.
    Любой залогиненный юзер может оценивать. Один юзер = одна оценка (можно переголосовать).
    При первой оценке победитель автоматически переезжает в /watched.

    Шкала 0.5-5 с шагом 0.5 (5 звёзд с половинками).
    """
    if not db._is_valid_rating(rating):
        return JSONResponse({"error": "rating must be 0.5-5 with 0.5 step"}, status_code=400)

    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    guild_id = get_current_guild_id(_user)
    success = await db.g_upsert_rating(guild_id, winner_id, user_discord_id, rating, review=review or None)
    if not success:
        return JSONResponse({"error": "failed to save rating"}, status_code=500)

    # Авто-выдача ачивок по триггеру ratings_count
    new_achievements = []
    try:
        new_achievements += await db.g_check_and_grant_auto(guild_id, user_discord_id, "ratings_count")
        new_achievements += await db.g_check_and_grant_auto(guild_id, user_discord_id, "first_rating")
    except Exception as e:
        import logging
        logging.getLogger("achievements").warning("auto-grant check failed: %s", e)

    avg, count = await db.g_get_average_rating(guild_id, winner_id)
    return JSONResponse({
        "ok": True,
        "winner_id": winner_id,
        "user_rating": rating,
        "avg_rating": avg,
        "ratings_count": count,
        "new_achievements": new_achievements,
    })


@app.get("/api/winners/{winner_id}/ratings")
async def api_get_ratings(
    winner_id: int,
    _user: dict = Depends(require_user),
):
    """Получить все оценки победителя."""
    guild_id = get_current_guild_id(_user)
    # get_ratings_for_winner - используем alias (он работает с guild_0), нужно сделать g_ версию
    table_r = db._guild.guild_table(guild_id, "ratings")
    async with db._connect() as conn:
        async with conn.execute(
            f"SELECT user_discord_id, rating, created_at, updated_at FROM {table_r} WHERE winner_id = ? ORDER BY created_at",
            (winner_id,)
        ) as cur:
            ratings = await cur.fetchall()
    user_discord_id = _user.get("discord_id", 0)
    user_rating = await db.g_get_user_rating(guild_id, winner_id, user_discord_id) if user_discord_id else None
    avg, count = await db.g_get_average_rating(guild_id, winner_id)
    return JSONResponse({
        "ratings": [
            {"user_id": r[0], "rating": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in ratings
        ],
        "avg_rating": avg,
        "ratings_count": count,
        "user_rating": user_rating,
    })


# === Watched page (для всех залогиненных) ===

@app.get("/watched", response_class=HTMLResponse)
async def watched_page(
    request: Request,
    _user: dict = Depends(require_user),
    q: str = "",
    page: int = 1,
    sort: str = "recent",
):
    guild_id = get_current_guild_id(_user)
    page = max(1, page)
    per_page = 20
    offset = (page - 1) * per_page
    search = q.strip() or None
    user_discord_id = _user.get("discord_id", 0)
    watched = await db.g_list_watched(
        guild_id, limit=per_page, offset=offset, search=search,
        user_discord_id=user_discord_id,
        sort=sort,
    )
    total_count = await db.g_count_watched(guild_id, search)
    total_pages = max(1, (total_count + per_page - 1) // per_page)

    # Предзагрузка постеров из кеша movie_meta (0 запросов к API)
    titles = [w[1] for w in watched]  # w = (id, title, watched_at, rating, avg, count, user_rating, winner_id)
    posters = await db.get_posters_for_titles(titles) if titles else {}

    return templates.TemplateResponse(request, "watched.html", {
        "user": _user,
        "watched": watched,
        "is_admin": _user.get("is_admin", False),
        "search": q,
        "sort": sort,
        "page": page,
        "per_page": per_page,
        "total": total_count,
        "total_pages": total_pages,
        "posters": posters,  # dict {lower_title: poster_url}
    })


@app.post("/api/watched/add")
async def api_add_to_watched(
    _user: dict = Depends(require_user),
    title: str = Form(...),
):
    """Добавить фильм в бэклог вручную (без оценки, без winner).
    Оценку можно поставить позже на странице /winners если создать winner.
    Любой юзер может добавлять.
    """
    title = title.strip()
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)

    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)

    # Проверяем дубликат (case-insensitive)
    if await db.g_is_watched(guild_id, title):
        return JSONResponse({"error": "already in backlog"}, status_code=409)

    # Добавляем в бэклог (без оценки)
    added = await db.g_add_watched(guild_id, title, None, user_discord_id)
    if not added:
        return JSONResponse({"error": "failed to add"}, status_code=500)

    # v2.3: Проверка авто-ачивок после добавления в бэклог
    try:
        await db.g_check_and_grant_auto(guild_id, user_discord_id, "watched_count")
    except Exception:
        pass

    return JSONResponse({"ok": True, "title": title})


@app.post("/watched/{watched_id}/delete")
async def delete_watched_endpoint(
    watched_id: int,
    _user: dict = Depends(require_admin),
):
    """Удалить запись из бэклога просмотренного (только админ)."""
    guild_id = get_current_guild_id(_user)
    deleted = await db.g_delete_watched(guild_id, watched_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return RedirectResponse(url="/watched?deleted=1", status_code=303)


@app.post("/api/watched/{watched_id}/edit")
async def api_edit_watched(
    watched_id: int,
    _user: dict = Depends(require_user),
    title: str = Form(""),
    rating: int | None = Form(None),
):
    """Редактировать название фильма в бэклоге.

    Параметр rating оставлен для обратной совместимости со старым JS-кодом,
    но больше не используется — оценки теперь per-user через /api/watched/{id}/rate.
    """
    guild_id = get_current_guild_id(_user)

    if title.strip():
        updated = await db.g_update_watched_title(guild_id, watched_id, title.strip())
        if not updated:
            return JSONResponse({"error": "not found"}, status_code=404)

    return JSONResponse({"ok": True})


@app.post("/api/watched/{watched_id}/rate")
async def api_rate_watched(
    watched_id: int,
    _user: dict = Depends(require_user),
    rating: float = Form(...),
    review: str = Form(""),
):
    """Поставить per-user оценку фильму в бэклоге (0.5-5 с половинками).

    Логика:
      1. Найти title фильма по watched_id.
      2. Найти или создать winner по title (если фильм не был победителем колеса —
         создаётся «виртуальный» unconfirmed winner, чтобы было к чему привязать оценки).
      3. g_upsert_rating — per-user оценка (один юзер = одна оценка, можно переголосовать).
      4. Вернуть avg_rating, ratings_count, user_rating для обновления UI без перезагрузки.
    """
    if not db._is_valid_rating(rating):
        return JSONResponse({"error": "rating must be 0.5-5 with 0.5 step"}, status_code=400)

    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    guild_id = get_current_guild_id(_user)

    # 1. Найти title по watched_id
    table_w = db._guild.guild_table(guild_id, "watched")
    async with db._connect() as conn:
        async with conn.execute(
            f"SELECT title FROM {table_w} WHERE id = ?", (watched_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return JSONResponse({"error": "watched not found"}, status_code=404)
    title = row[0]

    # 2. Найти или создать winner по title
    winner_id = await db.g_get_or_create_winner_by_title(guild_id, title)

    # 3. Per-user upsert оценки
    success = await db.g_upsert_rating(guild_id, winner_id, user_discord_id, rating, review=review or None)
    if not success:
        return JSONResponse({"error": "failed to save rating"}, status_code=500)

    # Авто-выдача ачивок по триггеру ratings_count
    new_achievements = []
    try:
        new_achievements += await db.g_check_and_grant_auto(guild_id, user_discord_id, "ratings_count")
        new_achievements += await db.g_check_and_grant_auto(guild_id, user_discord_id, "first_rating")
    except Exception as e:
        import logging
        logging.getLogger("achievements").warning("auto-grant check failed: %s", e)
    # 4. Вернуть агрегаты
    avg, count = await db.g_get_average_rating(guild_id, winner_id)
    return JSONResponse({
        "ok": True,
        "watched_id": watched_id,
        "winner_id": winner_id,
        "user_rating": rating,
        "avg_rating": avg,
        "ratings_count": count,
        "new_achievements": new_achievements,
    })


@app.get("/api/movie/poster")
async def api_movie_poster(
    _user: dict = Depends(require_user),
    title: str = "",
):
    """Получить постер фильма по названию.
    Сначала проверяет кеш movie_meta (0 запросов к API).
    Если не найден — делает 1 запрос к Кинопоиску, кеширует на 7 дней.
    Возвращает {poster_url, title, year, rating} или {poster_url: null}.
    """
    title = title.strip()
    if not title:
        return JSONResponse({"poster_url": None})

    # 1. Проверяем кеш (cache-first, без TTL — постеры не протухают)
    posters = await db.get_posters_for_titles([title])
    if title.lower() in posters:
        return JSONResponse({"poster_url": posters[title.lower()]})

    # 2. Нет в кеше — идём в Кинопоиск (1 запрос, сохранится в кеш)
    import kinopoisk as kp
    meta = await kp.lookup_movie(title)
    if meta and meta.get("poster_url"):
        return JSONResponse({
            "poster_url": meta["poster_url"],
            "title": meta.get("title"),
            "year": meta.get("year"),
            "rating": meta.get("vote_average"),
        })

    return JSONResponse({"poster_url": None})


# === Users management (admin only) ===

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, _user: dict = Depends(require_superuser)):
    users = await db.list_users()
    return templates.TemplateResponse(request, "users.html", {
        "user": _user,
        "users": users,
    })


# === Guilds admin page ===

@app.get("/guilds", response_class=HTMLResponse)
async def guilds_page(request: Request, _user: dict = Depends(require_superuser)):
    """Админ-страница управления серверами: approve/reject."""
    import guild as guild_module
    guilds = await guild_module.list_guilds(approved_only=False)
    return templates.TemplateResponse(request, "guilds.html", {
        "user": _user,
        "guilds": guilds,
    })


@app.post("/guilds/{guild_id}/approve")
async def approve_guild_endpoint(
    guild_id: int,
    _user: dict = Depends(require_superuser),
):
    """Одобрить сервер."""
    import guild as guild_module
    await guild_module.approve_guild(guild_id, _user.get("discord_id", 0))
    return RedirectResponse(url="/guilds?saved=1", status_code=303)


@app.post("/guilds/{guild_id}/reject")
async def reject_guild_endpoint(
    guild_id: int,
    _user: dict = Depends(require_superuser),
):
    """Отклонить сервер."""
    import guild as guild_module
    await guild_module.reject_guild(guild_id)
    return RedirectResponse(url="/guilds?saved=1", status_code=303)


@app.post("/guilds/{guild_id}/delete")
async def delete_guild_endpoint(
    guild_id: int,
    _user: dict = Depends(require_superuser),
):
    """Удалить сервер и все его данные (irreversible)."""
    import guild as guild_module
    if guild_id == 0:
        return RedirectResponse(url="/guilds?error=cannot_delete_default", status_code=303)
    await guild_module.drop_guild_tables(guild_id)
    # Удаляем запись из реестра
    async with db._connect() as conn:
        await conn.execute("DELETE FROM guilds WHERE guild_id = ?", (guild_id,))
        await conn.commit()
    return RedirectResponse(url="/guilds?deleted=1", status_code=303)


@app.post("/users/{discord_id}/admin")
async def toggle_admin(
    request: Request,
    discord_id: int,
    _user: dict = Depends(require_superuser),
    role: str = Form("user"),
):
    """Изменить роль юзера. Только superuser (Матка).
    Принимает role='user' (Пчела) или role='junior-admin' (Трутень).
    Superuser нельзя установить или изменить через эту форму — только через env.
    """
    # Валидация: принимаем только user и junior-admin
    if role not in ('user', 'junior-admin'):
        role = 'user'
    await db.set_user_role(discord_id, role)
    return RedirectResponse(url="/users?saved=1", status_code=303)


# === Quotes (страница + API) ===

@app.get("/quotes", response_class=HTMLResponse)
async def quotes_page(
    request: Request,
    _user: dict = Depends(require_user),
    q: str = "",
    page: int = 1,
):
    """Страница цитатника: список, поиск, форма создания, удаление."""
    guild_id = get_current_guild_id(_user)
    page = max(1, page)
    per_page = 20
    offset = (page - 1) * per_page
    search = q.strip() or None
    quotes = await db.g_list_quotes(guild_id, limit=per_page, offset=offset, search=search)
    total = await db.g_count_quotes(guild_id, search)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse(request, "quotes.html", {
        "user": _user,
        "quotes": quotes,
        "search": q,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "is_admin": _user.get("is_admin", False),
        "current_discord_id": _user.get("discord_id", 0),
    })


@app.post("/api/quotes/import-from-discord")
async def api_import_quotes_from_discord(_user: dict = Depends(require_admin)):
    """Импортировать существующие сообщения из Discord-канала #цитатник в БД.

    Логика:
    - Читаем последние 100 сообщений из channel_quotes_id
    - Пропускаем сообщения от ботов (чтобы не было цикла)
    - Для каждого сообщения:
      - author = display_name автора сообщения в Discord
      - text = содержимое сообщения
      - recorded_by = ID автора сообщения (кто отправил — тот и записал)
      - message_link = ссылка на сообщение
    - Пропускаем дубликаты (по message_link)
    """
    guild_id = get_current_guild_id(_user)

    # ID канала #цитатник из настроек
    quotes_channel_id_str = await db.get_setting("channel_quotes_id")
    if not quotes_channel_id_str or not quotes_channel_id_str.isdigit():
        return JSONResponse({"error": "Канал #цитатник не настроен в /channels"}, status_code=400)

    # Получаем инстанс бота
    import bot as bot_module
    bot_instance = bot_module.get_bot_instance()
    if not bot_instance:
        return JSONResponse({"error": "Бот не запущен"}, status_code=500)

    channel = bot_instance.get_channel(int(quotes_channel_id_str))
    if channel is None:
        return JSONResponse({"error": "Канал не найден ботом"}, status_code=404)

    # Предзагружаем существующие message_links для быстрой дедупликации
    existing_quotes = await db.g_list_quotes(guild_id, limit=500, search=None)
    existing_links = {q[7] for q in existing_quotes if q[7]}  # q[7] = message_link

    imported = 0
    skipped_bot = 0
    skipped_duplicate = 0
    skipped_empty = 0

    try:
        async for message in channel.history(limit=100, oldest_first=True):
            # Пропускаем сообщения ботов (чтобы не было цикла самоповтора)
            if message.author.bot:
                skipped_bot += 1
                continue

            text = message.content.strip()
            if not text:
                skipped_empty += 1
                continue  # пустое сообщение (например, только embed)

            # Ссылка на сообщение
            message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

            # Проверяем дубликат по message_link
            if message_link in existing_links:
                skipped_duplicate += 1
                continue

            # author = display_name автора сообщения в Discord
            author_display = message.author.display_name or message.author.name or "Неизвестен"

            # Создаём цитату
            await db.g_add_quote(
                guild_id,
                author=author_display,
                author_user_id=message.author.id,
                text=text,
                recorded_by=message.author.id,
                author_avatar_url=str(message.author.display_avatar.url) if message.author.display_avatar else None,
                message_link=message_link,
            )
            existing_links.add(message_link)
            imported += 1

    except Exception as e:
        import logging
        logging.getLogger("web").error("Quotes import failed: %s", e)
        return JSONResponse({"error": f"Ошибка импорта: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "imported": imported,
        "skipped_bot": skipped_bot,
        "skipped_duplicate": skipped_duplicate,
        "skipped_empty": skipped_empty,
    })


@app.post("/api/quotes")
async def api_create_quote(
    _user: dict = Depends(require_user),
    author: str = Form(...),
    text: str = Form(...),
    author_user_id: int | None = Form(None),
    message_link: str = Form(""),
):
    """Создать цитату через веб-панель.
    НЕ отправляет в Discord — только сохраняет в БД.
    Для постинга в Discord есть /funword.
    """
    author = author.strip()
    text = text.strip()
    message_link = message_link.strip() or None
    if not author or not text:
        return JSONResponse({"error": "author and text are required"}, status_code=400)

    recorded_by = _user.get("discord_id", 0)
    if not recorded_by:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    # Если есть author_user_id — пробуем достать аватар через бота
    author_avatar_url = None
    if author_user_id:
        try:
            import bot as bot_module
            _, member_info = await bot_module.is_guild_member(author_user_id)
            if member_info and member_info.get("avatar_url"):
                author_avatar_url = member_info["avatar_url"]
        except Exception:
            pass

    guild_id = get_current_guild_id(_user)
    quote_id = await db.g_add_quote(
        guild_id, author, author_user_id, text, recorded_by, author_avatar_url, message_link,
    )
    # Авто-выдача ачивок по триггеру quotes_count (для записавшего)
    new_achievements = []
    try:
        new_achievements += await db.g_check_and_grant_auto(guild_id, recorded_by, "quotes_count")
        new_achievements += await db.g_check_and_grant_auto(guild_id, recorded_by, "first_quote")
    except Exception as e:
        import logging
        logging.getLogger("achievements").warning("auto-grant check failed: %s", e)
    return JSONResponse({"ok": True, "id": quote_id, "new_achievements": new_achievements})


@app.get("/api/quotes/export")
async def api_export_quotes(_user: dict = Depends(require_user)):
    """Экспортировать ВСЕ цитаты как TXT файл (только текст, без автора)."""
    from fastapi.responses import PlainTextResponse

    guild_id = get_current_guild_id(_user)
    quotes = await db.g_list_quotes(guild_id, limit=10000, offset=0, search=None)

    # Каждая цитата — отдельная строка. Без автора, без метаданных.
    lines = []
    for q in quotes:
        text = q[4]  # q[4] = text
        lines.append(text)

    content = "\n".join(lines)

    return PlainTextResponse(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=quotes.txt"},
    )


@app.delete("/api/quotes/{quote_id}")
async def api_delete_quote(
    quote_id: int,
    _user: dict = Depends(require_user),
):
    """Удалить цитату. Правила:
    - Автор цитаты (recorded_by) может удалить свою
    - Админ может удалить любую
    """
    guild_id = get_current_guild_id(_user)
    quote = await db.g_get_quote(guild_id, quote_id)
    if not quote:
        return JSONResponse({"error": "not found"}, status_code=404)

    # quote tuple: (id, author, author_user_id, author_avatar_url, text, recorded_by, recorded_at, message_link)
    recorded_by = quote[5]
    user_discord_id = _user.get("discord_id", 0)
    is_admin = _user.get("is_admin", False)

    if recorded_by != user_discord_id and not is_admin:
        return JSONResponse({"error": "forbidden: only author or admin can delete"}, status_code=403)

    deleted = await db.g_delete_quote(guild_id, quote_id)
    return JSONResponse({"ok": deleted})


@app.post("/quotes/{quote_id}/delete")
async def delete_quote_endpoint(
    quote_id: int,
    _user: dict = Depends(require_user),
):
    """Удалить цитату через POST-форму (HTML редирект на /quotes)."""
    guild_id = get_current_guild_id(_user)
    quote = await db.g_get_quote(guild_id, quote_id)
    if not quote:
        return RedirectResponse(url="/quotes?error=not_found", status_code=303)

    recorded_by = quote[5]
    user_discord_id = _user.get("discord_id", 0)
    is_admin = _user.get("is_admin", False)

    if recorded_by != user_discord_id and not is_admin:
        return RedirectResponse(url="/quotes?error=forbidden", status_code=303)

    await db.g_delete_quote(guild_id, quote_id)
    return RedirectResponse(url="/quotes?deleted=1", status_code=303)


# === Profile (связь с TG) ===

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, _user: dict = Depends(require_user)):
    discord_id = _user.get("discord_id")
    guild_id = get_current_guild_id(_user)
    tg_link = await db.get_tg_link(discord_id) if discord_id else None
    tg_settings = await db.get_user_tg_settings(discord_id) if discord_id else {}
    is_tg_linked = await db.is_tg_linked(discord_id) if discord_id else False
    steam_profile = await db.get_user_steam_profile(discord_id) if discord_id else None
    # Список желаемого (только свой)
    watchlist = []
    if discord_id:
        import guild as guild_module
        # init_guild_tables уже вызывается при on_ready бота
        watchlist = await db.g_list_watchlist(guild_id, discord_id, include_watched=True)
    # v2.3.2: показывать ли Steam-вишлист в публичном профиле
    show_wishlist = await db.get_user_show_wishlist(discord_id) if discord_id else False
    return templates.TemplateResponse(request, "profile.html", {
        "user": _user,
        "tg_link": tg_link,
        "tg_settings": tg_settings,
        "is_tg_linked": is_tg_linked,
        "tg_notify_settings": db.TG_NOTIFY_SETTINGS,
        "steam_profile": steam_profile,
        "show_wishlist": show_wishlist,
        "is_santa_enabled": await is_santa_enabled(),
        "watchlist": watchlist,
    })


# === Публичный профиль (v1.9.0) ===

@app.get("/u/{discord_id}", response_class=HTMLResponse)
async def public_profile_page(
    request: Request,
    discord_id: int,
    _user: dict = Depends(require_user),
):
    """Публичный профиль пользователя. Виден всем залогиненным юзерам."""
    target = await db.get_user(discord_id)
    if not target:
        return templates.TemplateResponse(request, "profile_public.html", {
            "user": _user,
            "target": None,
            "not_found": True,
        }, status_code=404)

    guild_id = get_current_guild_id(_user)
    target_discord_id = target[0]
    username = target[1]
    display_name = target[2]
    is_target_admin = bool(target[3])
    avatar_url = target[4]
    top_role = target[6]
    guild_name = target[7]
    last_login_at = target[8]
    target_role = target[9] if len(target) > 9 else 'user'  # role column (v1.9.6)
    target_role_label = db.ROLE_LABELS.get(target_role, 'Пчела')

    # Статистика
    stats = await db.g_get_user_stats(guild_id, target_discord_id)
    # Последние оценки
    recent_ratings = await db.g_get_user_recent_ratings(guild_id, target_discord_id, limit=5)
    # Последние цитаты
    recent_quotes = await db.g_get_user_recent_quotes(guild_id, target_discord_id, limit=5)
    # Достижения (ачивки)
    user_achievements = await db.g_list_user_achievements(guild_id, target_discord_id, active_only=True)

    # Taste match с текущим юзером
    current_discord_id = _user.get("discord_id", 0)
    taste_match = None
    watched_together = 0
    if current_discord_id and current_discord_id != target_discord_id:
        taste_match = await db.g_get_taste_match(guild_id, current_discord_id, target_discord_id)
        watched_together = await db.g_get_watched_together_count(guild_id, current_discord_id, target_discord_id)

    is_self = (current_discord_id == target_discord_id)

    # v2.0: Discord data — статус, игра, войс
    discord_info = None
    voice_stats = None
    voice_co = None
    top_games = None
    try:
        import bot as bot_module
        discord_info = await bot_module.get_member_discord_info(target_discord_id, int(guild_id))
    except Exception:
        pass
    # Voice stats
    try:
        voice_stats = await db.g_get_voice_stats(guild_id, target_discord_id)
        if voice_stats["total_seconds"] > 0:
            voice_co = await db.g_get_voice_co_occurrence(guild_id, target_discord_id, limit=3)
            top_games = await db.g_get_top_games(guild_id, target_discord_id, limit=3)
    except Exception:
        pass

    # v2.0.3: Последние игры + game_compat
    recent_games = None
    game_compat = None
    try:
        recent_games = await db.g_get_user_recent_games(guild_id, target_discord_id, limit=5)
        if current_discord_id and current_discord_id != target_discord_id:
            game_compat = await db.g_get_game_compat(guild_id, current_discord_id, target_discord_id)
    except Exception:
        pass

    # v2.1.0: Steam игры (lazy refresh — кеш на 1 час, потом обновление из Steam API)
    recent_steam_games = None
    steam_profile_check = None
    try:
        steam_profile_check = await db.get_user_steam_profile(target_discord_id)
        if steam_profile_check and steam_profile_check.get("steam_id64"):
            # У юзера привязан Steam-профиль — получаем топ-5 недавно игр
            recent_steam_games = await db.g_get_user_steam_games(
                guild_id, target_discord_id, limit=5
            )
    except Exception as e:
        import logging
        logging.getLogger("steam").warning("Steam games fetch failed for %s: %s",
                                            target_discord_id, e)

    # v2.3: Merged games (Steam приоритет + Discord доп) + pinned achievements
    merged_games = None
    pinned_achievements = None
    try:
        merged_games = await db.g_get_user_games_merged(guild_id, target_discord_id, limit=10)
    except Exception as e:
        import logging
        logging.getLogger("games").warning("Merged games fetch failed for %s: %s", target_discord_id, e)
    try:
        pinned_achievements = await db.g_get_pinned_achievements(guild_id, target_discord_id)
    except Exception:
        pass

    # v2.0.3: Если юзер смотрит свой профиль — отмечаем ачивки как просмотренные
    if is_self and current_discord_id:
        try:
            await db.g_mark_achievements_viewed(current_discord_id)
        except Exception:
            pass

    # v2.3.2: Steam-вишлист (если юзер разрешил показ)
    steam_wishlist = None
    try:
        if steam_profile_check and steam_profile_check.get("steam_id64"):
            show_wl = await db.get_user_show_wishlist(target_discord_id)
            if show_wl:
                import steam as steam_module
                api_key = await steam_module.get_steam_api_key()
                if api_key:
                    wl = await steam_module.get_top_wishlist_games(
                        steam_profile_check["steam_id64"], limit=10
                    )
                    steam_wishlist = wl if wl else []
    except Exception as e:
        import logging
        logging.getLogger("wishlist").warning(
            "Wishlist fetch failed for %s: %s", target_discord_id, e
        )

    # v3.1.5: Village Quest — fetch MC player data if linked via /vq
    # Best-effort — если MC сервер спит (exarathon) или мод не настроен,
    # просто не показываем секцию.
    vq_player = None
    vq_link = None
    try:
        # Локальный кеш линков — быстрый, без обращения к моду
        import vq_data
        vq_link = await vq_data.get_link_by_discord(guild_id, target_discord_id)
        if vq_link:
            # Линк есть — пробуем получить карточку с сервера (transport-agnostic)
            import vq_api
            result = await vq_api.get_player_by_discord(guild_id, target_discord_id)
            if result.get("status") == 200 and isinstance(result.get("result"), dict):
                # Может быть либо чистый player dict, либо обёртка {discord_id, link, player}
                r = result["result"]
                vq_player = r.get("player") if isinstance(r, dict) and "player" in r else r
            elif result.get("error") == "timeout":
                # MC сервер спит — сохраняем что линк есть, карточку не покажем
                logging.getLogger("vq").debug(
                    "VQ player fetch timeout for %s (server sleeping?)",
                    target_discord_id,
                )
            # Иначе — мод ответил ошибкой (404 не должно быть, линк есть в кеше),
            # просто не показываем секцию
    except Exception as e:
        import logging
        logging.getLogger("vq").debug(
            "VQ player fetch failed for %s: %s", target_discord_id, e
        )

    return templates.TemplateResponse(request, "profile_public.html", {
        "user": _user,
        "target": {
            "discord_id": target_discord_id,
            "username": username,
            "display_name": display_name or username,
            "avatar_url": avatar_url,
            "is_admin": is_target_admin,
            "is_superuser": target_role == 'superuser',
            "role": target_role,
            "role_label": target_role_label,
            "top_role": top_role,
            "guild_name": guild_name,
            "last_login_at": last_login_at,
        },
        "stats": stats,
        "recent_ratings": recent_ratings,
        "recent_quotes": recent_quotes,
        "user_achievements": user_achievements,
        "taste_match": taste_match,
        "watched_together": watched_together,
        "is_self": is_self,
        "not_found": False,
        "discord_info": discord_info,
        "voice_stats": voice_stats,
        "voice_co": voice_co,
        "top_games": top_games,
        "recent_games": recent_games,
        "game_compat": game_compat,
        "recent_steam_games": recent_steam_games,
        "has_steam_linked": bool(steam_profile_check and steam_profile_check.get("steam_id64")),
        "merged_games": merged_games,
        "pinned_achievements": pinned_achievements,
        "steam_wishlist": steam_wishlist,
        "vq_link": vq_link,
        "vq_player": vq_player,
    })


@app.get("/api/winners/{winner_id}/ratings_list")
async def api_get_ratings_list(
    winner_id: int,
    _user: dict = Depends(require_user),
):
    """Список всех оценок победителя (для модалки «кто как оценил»).

    Возвращает [{user_discord_id, username, display_name, avatar_url, rating, updated_at}, ...].
    """
    guild_id = get_current_guild_id(_user)
    ratings = await db.g_get_ratings_for_winner(guild_id, winner_id)
    return JSONResponse({"ratings": ratings, "count": len(ratings)})


# === Watchlist API ===

@app.post("/api/watchlist/add")
async def api_watchlist_add(
    _user: dict = Depends(require_user),
    title: str = Form(...),
):
    """Добавить фильм в личный список желаемого.
    Проверяет дубликаты по tmdb_id (или по title если tmdb_id нет) среди ВСЕХ юзеров guild.
    """
    title = title.strip()
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)
    # init_guild_tables уже вызывается при on_ready бота
    # Ищем метаданные
    import kinopoisk as kp
    meta = await kp.lookup_movie(title)
    name = meta["title"] if meta else title
    tmdb_id = meta["tmdb_id"] if meta else None
    watchlist_id, error = await db.g_add_to_watchlist(guild_id, user_discord_id, name, tmdb_id)
    if error == "already_in_other_watchlist":
        return JSONResponse({
            "error": f"Фильм «{name}» уже кем-то добавлен в список желаемого. Это сюрприз для колеса — нельзя иметь у двух людей сразу.",
            "error_code": "already_in_other_watchlist",
        }, status_code=409)
    if error == "already_yours":
        return JSONResponse({
            "error": f"Фильм «{name}» уже в вашем списке желаемого.",
            "error_code": "already_yours",
        }, status_code=409)
    # v2.3: Проверка авто-ачивок после добавления в вишлист
    try:
        await db.g_check_and_grant_auto(guild_id, user_discord_id, "watchlist_count")
    except Exception:
        pass
    return JSONResponse({"ok": True, "id": watchlist_id, "title": name})


@app.delete("/api/watchlist/{watchlist_id}")
async def api_watchlist_delete(
    watchlist_id: int,
    _user: dict = Depends(require_user),
):
    """Удалить фильм из списка желаемого (только владелец)."""
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    deleted = await db.g_remove_from_watchlist(guild_id, watchlist_id, user_discord_id)
    if not deleted:
        return JSONResponse({"error": "not found or not yours"}, status_code=404)
    return JSONResponse({"ok": True})


@app.post("/api/watchlist/{watchlist_id}/edit")
async def api_watchlist_edit(
    watchlist_id: int,
    _user: dict = Depends(require_user),
    title: str = Form(...),
):
    """Изменить название фильма в списке желаемого (только владелец)."""
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    updated = await db.g_update_watchlist_title(guild_id, watchlist_id, user_discord_id, title)
    if not updated:
        return JSONResponse({"error": "not found or not yours"}, status_code=404)
    return JSONResponse({"ok": True})


# === TG Notification settings API (v1.7.4) ===

@app.post("/api/profile/tg_settings")
async def api_set_tg_settings(
    _user: dict = Depends(require_user),
    setting_key: str = Form(...),
    value: bool = Form(False),
):
    """Установить одну настройку TG-уведомлений.
    Требует привязки TG (иначе зачем).
    """
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    if not await db.is_tg_linked(user_discord_id):
        return JSONResponse({
            "error": "Telegram не привязан. Используйте /linktg в Discord чтобы привязать аккаунт.",
            "error_code": "not_linked",
        }, status_code=400)

    try:
        updated = await db.set_user_tg_setting(user_discord_id, setting_key, value)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    return JSONResponse({"ok": updated, "setting_key": setting_key, "value": value})


# v2.3.2: показать/скрыть Steam-вишлист в публичном профиле
@app.post("/api/profile/show_wishlist")
async def api_set_show_wishlist(
    _user: dict = Depends(require_user),
    value: bool = Form(False),
):
    """Переключатель show_wishlist: показывать ли Steam-вишлист в публичном профиле.

    Работает только если у юзера задан Steam-профиль.
    """
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)
    if not await db.is_steam_profile_set(user_discord_id):
        return JSONResponse({
            "error": "Сначала укажите Steam-профиль.",
            "error_code": "no_steam",
        }, status_code=400)
    updated = await db.set_user_show_wishlist(user_discord_id, value)
    return JSONResponse({"ok": updated, "value": value})


@app.post("/api/profile/steam")
async def api_set_steam_profile(
    _user: dict = Depends(require_user),
    steam_profile_url: str = Form(...),
):
    """Сохранить Steam-профиль юзера.
    Валидирует через Steam API, сохраняет steam_id64, persona, avatar в users.
    """
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    import steam
    validation = await steam.validate_steam_profile(steam_profile_url.strip())
    if not validation["valid"]:
        return JSONResponse({
            "error": validation["error"] or "Steam-профиль невалиден.",
            "error_code": "invalid_steam_profile",
        }, status_code=400)

    await db.set_user_steam_profile(
        user_discord_id,
        validation["steam_profile_url"],
        validation["steam_id64"],
        validation["steam_persona"],
        validation["steam_avatar_url"],
    )

    return JSONResponse({
        "ok": True,
        "steam_persona": validation["steam_persona"],
        "steam_avatar_url": validation["steam_avatar_url"],
        "steam_profile_url": validation["steam_profile_url"],
        "wishlist_public": validation["wishlist_public"],
    })


@app.delete("/api/profile/steam")
async def api_delete_steam_profile(_user: dict = Depends(require_user)):
    """Очистить Steam-профиль юзера."""
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)
    await db.set_user_steam_profile(user_discord_id, None, None, None, None)
    return JSONResponse({"ok": True})


# === Wheel (собственное колесо в панели) ===

# v2.3.3: santa и achievements роуты вынесены в отдельные модули
import santa_routes
import achievements_routes
import modrinth_routes
app.include_router(santa_routes.router)
app.include_router(achievements_routes.router)
app.include_router(modrinth_routes.router)

