"""FastAPI веб-панель: логин по Discord ID или логин/пароль, права is_admin."""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

import crypto
import db
import ws_manager
from config import settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 дней

serializer = URLSafeTimedSerializer(settings.app_secret, salt="panel-session")

app = FastAPI(title="Kinovecher Panel", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# === Session ===

def create_session(payload: dict) -> str:
    return serializer.dumps({"u": payload, "t": datetime.utcnow().isoformat()})


def read_session(token: str) -> dict | None:
    """Возвращает payload (dict) из сессии или None."""
    try:
        data = serializer.loads(token, max_age=SESSION_TTL)
        # В старом формате data = {"u": "admin_string", "t": ...}, новый формат data = {"u": dict, "t": ...}
        # Поддерживаем оба: возвращаем только dict-payload, старый string-формат игнорируем
        user = data.get("u") if isinstance(data, dict) else None
        if isinstance(user, dict):
            return user
        return None
    except BadSignature:
        return None


async def get_current_user(request: Request) -> dict | None:
    """Возвращает user dict из сессии, или None если сессия невалидна/протухла/старого формата."""
    token = request.cookies.get("session")
    if not token:
        return None
    return read_session(token)


async def require_user(request: Request) -> dict:
    """Зависимость: юзер обязан быть залогинен. Иначе редирект на /login."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


async def require_admin(request: Request) -> dict:
    """Зависимость: юзер обязан быть админом. Иначе 403."""
    user = await require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ только для администратора")
    return user


# === Routes ===

@app.on_event("startup")
async def _startup():
    await db.init_db()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/theme")
async def set_theme(theme: str = Form(...)):
    """Установить тему (light/dark). Записывает куку и редиректит обратно."""
    if theme not in ("light", "dark"):
        theme = "light"
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


def _theme(request: Request) -> str:
    """Текущая тема из куки (light по умолчанию)."""
    return request.cookies.get("theme", "light")


# Регистрируем глобально для всех шаблонов, чтобы не передавать явно
templates.env.globals["theme_from_request"] = lambda request: request.cookies.get("theme", "light")


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
    """Логин тремя способами:
    1. Логин = ADMIN_LOGIN из env (текст, не число), пароль = ADMIN_PASSWORD — env-админ по логину/паролю
    2. Логин = Discord ID админа (env ADMIN_DISCORD_ID), пароль = ADMIN_PASSWORD — env-админ по Discord ID
    3. Логин = любой Discord ID (число), пароль пустой — авто-создание юзера с правами viewer

    Для входа по Discord ID бот должен быть онлайн и проверить что юзер — участник сервера.
    """
    login = login.strip()
    password = password.strip()

    is_env_admin_pass = secrets.compare_digest(password, settings.admin_password)

    # Способ 1: классический env-админ по логину+паролю (login="admin", password="changeme")
    # Только если login не числовой — иначе попадает в способ 2/3 ниже
    is_env_admin_login = secrets.compare_digest(login, settings.admin_login)
    if is_env_admin_login and is_env_admin_pass and not login.isdigit():
        user_payload = {"discord_id": 0, "username": login, "is_admin": True, "avatar_url": None}
        token = create_session(user_payload)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", token, max_age=SESSION_TTL, httponly=True, samesite="lax")
        return resp

    # Способ 2 и 3: вход по Discord ID (число)
    if login.isdigit():
        discord_id = int(login)

        # Проверка участника сервера через бота (только если бот онлайн)
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

        # Проверка прав
        is_admin = await db.is_admin(discord_id)
        # Если это Discord ID админа (env ADMIN_DISCORD_ID) — требуем пароль
        if settings.admin_discord_id is not None and discord_id == settings.admin_discord_id:
            if not is_env_admin_pass:
                return RedirectResponse(url="/login?error=admin_password", status_code=303)
            is_admin = True

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

        user_payload = {
            "discord_id": discord_id,
            "username": display_name,
            "is_admin": is_admin,
            "avatar_url": avatar_url,
            "roles": roles,
            "top_role": top_role,
            "guild_name": guild_name,
        }
        token = create_session(user_payload)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", token, max_age=SESSION_TTL, httponly=True, samesite="lax")
        return resp

    return RedirectResponse(url="/login?error=invalid", status_code=303)


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session")
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _user: dict = Depends(require_user)):
    recent_watched = await db.list_watched(limit=10)
    recent_winners = await db.list_winners(limit=10)
    activity = await db.recent_activity(limit=15)
    top_quotes = await db.top_quotes(limit=5)

    # Статусы токенов для виджетов
    saved_kp = bool(await db.get_setting("kinopoisk_token"))
    saved_tg = bool(await db.get_setting("telegram_token"))

    return templates.TemplateResponse(request, "panel.html", {
        "user": _user,
        "recent_watched": recent_watched,
        "recent_winners": recent_winners,
        "activity": activity,
        "top_quotes": top_quotes,
        "saved_kp": saved_kp,
        "saved_tg": saved_tg,
    })


# === Tokens (admin only) ===

KNOWN_TOKENS = [
    ("discord_token", "Discord Bot Token"),
    ("telegram_token", "Telegram Bot Token"),
    ("kinopoisk_token", "Kinopoisk API Token (X-API-KEY от @poiskkinodev_bot)"),
]


@app.get("/tokens", response_class=HTMLResponse)
async def tokens_form(request: Request, _user: dict = Depends(require_admin)):
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
    _user: dict = Depends(require_admin),
    discord_token: str = Form(""),
    telegram_token: str = Form(""),
    kinopoisk_token: str = Form(""),
):
    updates = {
        "discord_token": discord_token.strip(),
        "telegram_token": telegram_token.strip(),
        "kinopoisk_token": kinopoisk_token.strip(),
    }
    for key, val in updates.items():
        if val:
            encrypted = crypto.encrypt(val)
            await db.set_setting(key, encrypted, is_secret=True)
    return RedirectResponse(url="/tokens?saved=1", status_code=303)


# === Channels (admin only) ===

KNOWN_CHANNELS = [
    ("channel_quotes_id", "ID канала #цитатник"),
    ("channel_announce_id", "ID канала анонсов киновечера"),
    ("channel_winners_id", "ID канала #winners (куда постить победителей колеса)"),
    ("role_movie_ping_id", "ID роли для пинга анонсов"),
    ("telegram_chat_id", "Telegram chat_id для бэклога просмотренных"),
    ("tg_winners_chat_id", "Telegram chat_id для победителей колеса (опционально)"),
]


@app.get("/channels", response_class=HTMLResponse)
async def channels_form(request: Request, _user: dict = Depends(require_admin)):
    values = {key: (await db.get_setting(key) or "") for key, _label in KNOWN_CHANNELS}
    return templates.TemplateResponse(request, "channels.html", {
        "user": _user,
        "channels": KNOWN_CHANNELS,
        "values": values,
    })


@app.post("/channels")
async def channels_save(
    request: Request,
    _user: dict = Depends(require_admin),
    channel_quotes_id: str = Form(""),
    channel_announce_id: str = Form(""),
    channel_winners_id: str = Form(""),
    role_movie_ping_id: str = Form(""),
    telegram_chat_id: str = Form(""),
    tg_winners_chat_id: str = Form(""),
):
    updates = {
        "channel_quotes_id": channel_quotes_id.strip(),
        "channel_announce_id": channel_announce_id.strip(),
        "channel_winners_id": channel_winners_id.strip(),
        "role_movie_ping_id": role_movie_ping_id.strip(),
        "telegram_chat_id": telegram_chat_id.strip(),
        "tg_winners_chat_id": tg_winners_chat_id.strip(),
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
]


@app.get("/features", response_class=HTMLResponse)
async def features_form(request: Request, _user: dict = Depends(require_user)):
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
    _user: dict = Depends(require_admin),
    tg_crosspost_quotes: bool = Form(False),
    tg_crosspost_announce: bool = Form(False),
):
    await db.set_setting("tg_crosspost_quotes", "1" if tg_crosspost_quotes else "0", is_secret=False)
    await db.set_setting("tg_crosspost_announce", "1" if tg_crosspost_announce else "0", is_secret=False)
    return RedirectResponse(url="/features?saved=1", status_code=303)


# === Winners page (для всех залогиненных) ===

@app.get("/winners", response_class=HTMLResponse)
async def winners_page(request: Request, _user: dict = Depends(require_user)):
    """Победители с агрегированными рейтингами."""
    winners = await db.get_winners_with_ratings(limit=50)
    user_discord_id = _user.get("discord_id", 0)
    # Для каждого победителя — оценка текущего юзера (для подсветки звёзд)
    for w in winners:
        w["user_rating"] = await db.get_user_rating(w["id"], user_discord_id) if user_discord_id else None
    return templates.TemplateResponse(request, "winners.html", {
        "user": _user,
        "winners": winners,
        "is_admin": _user.get("is_admin", False),
    })


@app.post("/winners/{winner_id}/delete")
async def delete_winner_endpoint(
    winner_id: int,
    _user: dict = Depends(require_admin),
):
    """Удалить запись победителя (только админ)."""
    deleted = await db.delete_winner(winner_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return RedirectResponse(url="/winners?deleted=1", status_code=303)


@app.post("/api/winners/{winner_id}/rate")
async def api_rate_winner(
    winner_id: int,
    _user: dict = Depends(require_user),
    rating: int = Form(...),
):
    """Поставить или обновить оценку победителю.
    Любой залогиненный юзер может оценивать. Один юзер = одна оценка (можно переголосовать).
    При первой оценке победитель автоматически переезжает в /watched.
    """
    if not (1 <= rating <= 10):
        return JSONResponse({"error": "rating must be 1-10"}, status_code=400)

    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    success = await db.upsert_rating(winner_id, user_discord_id, rating)
    if not success:
        return JSONResponse({"error": "failed to save rating"}, status_code=500)

    avg, count = await db.get_average_rating(winner_id)
    return JSONResponse({
        "ok": True,
        "winner_id": winner_id,
        "user_rating": rating,
        "avg_rating": avg,
        "ratings_count": count,
    })


@app.get("/api/winners/{winner_id}/ratings")
async def api_get_ratings(
    winner_id: int,
    _user: dict = Depends(require_user),
):
    """Получить все оценки победителя (для показа кто как оценил)."""
    ratings = await db.get_ratings_for_winner(winner_id)
    user_discord_id = _user.get("discord_id", 0)
    user_rating = await db.get_user_rating(winner_id, user_discord_id) if user_discord_id else None
    avg, count = await db.get_average_rating(winner_id)
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
async def watched_page(request: Request, _user: dict = Depends(require_user)):
    watched = await db.list_watched(limit=100)
    return templates.TemplateResponse(request, "watched.html", {
        "user": _user,
        "watched": watched,
        "is_admin": _user.get("is_admin", False),
    })


@app.post("/watched/{watched_id}/delete")
async def delete_watched_endpoint(
    watched_id: int,
    _user: dict = Depends(require_admin),
):
    """Удалить запись из бэклога просмотренного (только админ)."""
    deleted = await db.delete_watched(watched_id)
    if not deleted:
        return JSONResponse({"error": "not found"}, status_code=404)
    return RedirectResponse(url="/watched?deleted=1", status_code=303)


# === Users management (admin only) ===

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, _user: dict = Depends(require_admin)):
    users = await db.list_users()
    return templates.TemplateResponse(request, "users.html", {
        "user": _user,
        "users": users,
    })


@app.post("/users/{discord_id}/admin")
async def toggle_admin(
    request: Request,
    discord_id: int,
    _user: dict = Depends(require_admin),
    make_admin: bool = Form(False),
):
    await db.set_admin(discord_id, make_admin)
    return RedirectResponse(url="/users?saved=1", status_code=303)


# === Profile (связь с TG) ===

@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, _user: dict = Depends(require_user)):
    discord_id = _user.get("discord_id")
    tg_link = await db.get_tg_link(discord_id) if discord_id else None
    return templates.TemplateResponse(request, "profile.html", {
        "user": _user,
        "tg_link": tg_link,
    })


# === Wheel (собственное колесо в панели) ===

@app.get("/wheel", response_class=HTMLResponse)
async def wheel_page(request: Request, _user: dict = Depends(require_user)):
    """Страница с Canvas-анимацией колеса. Для стримера — открыть на отдельном мониторе."""
    items = await db.list_wheel_items(active_only=True)
    return templates.TemplateResponse(request, "wheel.html", {
        "user": _user,
        "items": items,
    })


@app.get("/api/wheel/items")
async def api_wheel_items(_user: dict = Depends(require_user)):
    """Получить текущие лоты колеса (JSON)."""
    items = await db.list_wheel_items(active_only=True)
    return JSONResponse({"items": items, "count": len(items)})


@app.post("/api/wheel/items")
async def api_add_wheel_item(
    request: Request,
    _user: dict = Depends(require_user),
    name: str = Form(...),
    tmdb_id: int | None = Form(None),
):
    """Добавить лот в колесо. Триггерит broadcast всем WS-клиентам."""
    name = name.strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    discord_id = _user.get("discord_id", 0)
    item_id = await db.add_wheel_item(name, tmdb_id, discord_id)
    items = await db.list_wheel_items(active_only=True)
    await ws_manager.broadcast_wheel_updated(items)
    return JSONResponse({"id": item_id, "items": items, "count": len(items)})


@app.delete("/api/wheel/items/{item_id}")
async def api_remove_wheel_item(
    item_id: int,
    _user: dict = Depends(require_user),
):
    """Удалить лот (soft-delete). Триггерит broadcast."""
    removed = await db.remove_wheel_item(item_id)
    if not removed:
        return JSONResponse({"error": "not found"}, status_code=404)
    await db.reassign_colors()
    items = await db.list_wheel_items(active_only=True)
    await ws_manager.broadcast_wheel_updated(items)
    return JSONResponse({"removed": True, "items": items, "count": len(items)})


@app.post("/api/wheel/clear")
async def api_clear_wheel(_user: dict = Depends(require_admin)):
    """Очистить всё колесо (только админ)."""
    count = await db.clear_wheel()
    items = await db.list_wheel_items(active_only=True)
    await ws_manager.broadcast_wheel_updated(items)
    return JSONResponse({"cleared": count, "items": items})


@app.post("/api/wheel/spin")
async def api_spin_wheel(_user: dict = Depends(require_user)):
    """Запустить спин. Сервер выбирает победителя и рассылает результат через WS."""
    import random
    items = await db.list_wheel_items(active_only=True)
    if len(items) < 2:
        return JSONResponse({"error": "need at least 2 items to spin"}, status_code=400)

    # Случайный победитель
    winner = random.choice(items)
    spin_id = str(uuid.uuid4())[:8]

    # Рассылаем событие спина (клиенты начинают анимацию)
    await ws_manager.broadcast_spin_started(items, spin_id)

    # Записываем в БД как confirmed (мы точно знаем победителя)
    await db.add_winner(
        lot_id=str(winner["id"]),
        lot_name=winner["name"],
        tmdb_id=winner.get("tmdb_id"),
        confidence="confirmed",
    )

    # Удаляем победителя из колеса (он уже не участвует в следующем спине)
    await db.remove_wheel_item(winner["id"])
    await db.reassign_colors()

    # Финальный список после удаления победителя
    remaining = await db.list_wheel_items(active_only=True)

    # Задержка чтобы дать анимации докрутиться (5 секунд)
    import asyncio
    asyncio.create_task(_delayed_spin_result(winner, remaining, spin_id))

    return JSONResponse({
        "spin_id": spin_id,
        "winner": winner,
        "remaining_count": len(remaining),
    })


async def _delayed_spin_result(winner: dict, remaining: list[dict], spin_id: str) -> None:
    """Через 5 секунд разослать финальный результат спина."""
    import asyncio
    await asyncio.sleep(5)
    await ws_manager.broadcast_spin_result(winner, spin_id)
    # Также обновить список лотов (победитель удалён)
    await ws_manager.broadcast_wheel_updated(remaining)

    # Уведомить Discord через бота
    try:
        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if bot:
            await bot.on_wheel_spin_completed(winner)
    except Exception as e:
        import logging
        logging.getLogger("ws_manager").warning("Discord notify failed: %s", e)


@app.post("/api/wheel/spin_elimination")
async def api_spin_wheel_elimination(_user: dict = Depends(require_user)):
    """Режим 'на выбывание': спин удаляет ОДНОГО случайного лота.
    Когда остаётся 1 лот — он финальный победитель.

    Логика:
    - Если осталось >2 лотов: удаляем случайного, возвращаем updated list
    - Если осталось 2 лота: удаляем случайного, оставшийся становится победителем
      (с confidence='confirmed', уведомление в Discord, удаление из колеса)
    - Если <2: ошибка
    """
    import random
    items = await db.list_wheel_items(active_only=True)
    if len(items) < 2:
        return JSONResponse({"error": "need at least 2 items"}, status_code=400)

    spin_id = str(uuid.uuid4())[:8]

    # Если ровно 2 — финальный раунд, победитель = тот кто ОСТАЛСЯ
    if len(items) == 2:
        # Случайно удаляем одного — оставшийся победитель
        eliminated = random.choice(items)
        winner = next(it for it in items if it["id"] != eliminated["id"])

        await db.remove_wheel_item(eliminated["id"])
        await db.remove_wheel_item(winner["id"])
        await db.reassign_colors()

        # Записываем победителя в winners
        await db.add_winner(
            lot_id=str(winner["id"]),
            lot_name=winner["name"],
            tmdb_id=winner.get("tmdb_id"),
            confidence="confirmed",
        )

        # Рассылаем события
        await ws_manager.broadcast_spin_started(items, spin_id)
        remaining = []  # колесо пустое

        import asyncio
        asyncio.create_task(_delayed_spin_result(winner, remaining, spin_id))

        return JSONResponse({
            "spin_id": spin_id,
            "mode": "elimination_final",
            "eliminated": eliminated,
            "winner": winner,
            "remaining_count": 0,
        })

    # Обычный раунд выбывания — удаляем одного случайного
    eliminated = random.choice(items)
    await db.remove_wheel_item(eliminated["id"])
    await db.reassign_colors()
    remaining = await db.list_wheel_items(active_only=True)

    # Broadcast: спин начался (для анимации), затем обновлённый список
    await ws_manager.broadcast_spin_started(items, spin_id)

    import asyncio
    asyncio.create_task(_delayed_elimination_result(eliminated, remaining, spin_id))

    return JSONResponse({
        "spin_id": spin_id,
        "mode": "elimination",
        "eliminated": eliminated,
        "remaining_count": len(remaining),
    })


async def _delayed_elimination_result(eliminated: dict, remaining: list[dict], spin_id: str) -> None:
    """Через 5 секунд разослать результат elimination-спина (кого удалили)."""
    import asyncio
    await asyncio.sleep(5)
    await ws_manager.broadcast(
        "elimination_result",
        {"eliminated": eliminated, "remaining_count": len(remaining)},
    )
    await ws_manager.broadcast_wheel_updated(remaining)


# === WebSocket для реал-тайм обновлений ===

@app.websocket("/ws/wheel")
async def ws_wheel(websocket: WebSocket):
    """WebSocket для подписки на обновления колеса."""
    import json
    await ws_manager.connect(websocket)
    try:
        # При первом подключении сразу шлём текущее состояние
        items = await db.list_wheel_items(active_only=True)
        await websocket.send_text(json.dumps({
            "type": "wheel_updated",
            "payload": {"items": items, "count": len(items)},
        }, ensure_ascii=False))
        # Держим соединение, ждём сообщений (клиент может слать ping)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        import logging
        logging.getLogger("ws_manager").debug("WS error: %s", e)
        await ws_manager.disconnect(websocket)
