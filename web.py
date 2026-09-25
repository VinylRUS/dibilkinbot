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


def get_current_guild_id(user_payload: dict) -> int:
    """Текущий выбранный сервер из сессии пользователя. По умолчанию 0 (default)."""
    return user_payload.get("current_guild_id", 0)


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
        # Даже при текстовом логине — определяем guild_id через ADMIN_DISCORD_ID
        admin_guild_id = 0
        if settings.admin_discord_id:
            try:
                import bot as bot_module
                is_member, member_info = await bot_module.is_guild_member(settings.admin_discord_id)
                if is_member and member_info:
                    admin_guild_id = member_info.get("guild_id", 0)
                    # Регистрируем guild
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
            "avatar_url": None,
            "current_guild_id": admin_guild_id,
        }
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
        # Авто-определение guild_id из Discord (где бот нашёл этого юзера)
        member_guild_id = member_info.get("guild_id", 0)

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

        # Если guild не зарегистрирован в реестре — регистрируем и создаём таблицы
        if member_guild_id:
            import guild as guild_module
            try:
                await guild_module.init_guild_tables(member_guild_id)
                # Если это первый вход админа — авто-апрув
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

        user_payload = {
            "discord_id": discord_id,
            "username": display_name,
            "is_admin": is_admin,
            "avatar_url": avatar_url,
            "roles": roles,
            "top_role": top_role,
            "guild_name": guild_name,
            "current_guild_id": member_guild_id,  # ← АВТО-ВЫБОР реального guild при логине
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
    resp.set_cookie("session", token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _user: dict = Depends(require_user)):
    guild_id = get_current_guild_id(_user)
    recent_watched = await db.g_list_watched(guild_id, limit=10)
    recent_winners = await db.g_list_winners(guild_id, limit=10)
    activity = await db.g_recent_activity(guild_id, limit=15)
    top_quotes = await db.g_top_quotes(guild_id, limit=5)

    # Статусы токенов (глобальные)
    saved_kp = bool(await db.get_setting("kinopoisk_token"))
    saved_tg = bool(await db.get_setting("telegram_token"))

    # Текущий guild для отображения
    import guild as guild_module
    current_guild = await guild_module.get_guild(guild_id)
    available_guilds = await get_user_guilds(_user)

    return templates.TemplateResponse(request, "panel.html", {
        "user": _user,
        "current_guild_id": guild_id,
        "current_guild": current_guild,
        "available_guilds": available_guilds,
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
    ("kinopoisk_token", "Kinopoisk API Token (kinopoiskapiunofficial.tech)"),
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
    ("panel_base_url", "URL веб-панели (для кнопок в Telegram, например https://your-bot.bothost.tech)"),
    ("channel_quotes_id", "ID канала #цитатник"),
    ("channel_announce_id", "ID канала анонсов киновечера"),
    ("channel_winners_id", "ID канала #winners (куда постить победителей колеса)"),
    ("role_movie_ping_id", "ID роли для пинга анонсов"),
    ("telegram_chat_id", "Telegram chat_id для бэклога просмотренных"),
    ("telegram_thread_id", "Telegram thread_id (ID темы форума, опционально)"),
    ("tg_winners_chat_id", "Telegram chat_id для победителей колеса (опционально)"),
    ("tg_winners_thread_id", "Telegram thread_id для победителей колеса (опционально)"),
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
    panel_base_url: str = Form(""),
    channel_quotes_id: str = Form(""),
    channel_announce_id: str = Form(""),
    channel_winners_id: str = Form(""),
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
    guild_id = get_current_guild_id(_user)
    winners = await db.g_get_winners_with_ratings(guild_id, limit=50)
    user_discord_id = _user.get("discord_id", 0)
    for w in winners:
        w["user_rating"] = await db.g_get_user_rating(guild_id, w["id"], user_discord_id) if user_discord_id else None
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
    guild_id = get_current_guild_id(_user)
    deleted = await db.g_delete_winner(guild_id, winner_id)
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

    guild_id = get_current_guild_id(_user)
    success = await db.g_upsert_rating(guild_id, winner_id, user_discord_id, rating)
    if not success:
        return JSONResponse({"error": "failed to save rating"}, status_code=500)

    avg, count = await db.g_get_average_rating(guild_id, winner_id)
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
async def watched_page(request: Request, _user: dict = Depends(require_user)):
    guild_id = get_current_guild_id(_user)
    watched = await db.g_list_watched(guild_id, limit=100)
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
    guild_id = get_current_guild_id(_user)
    deleted = await db.g_delete_watched(guild_id, watched_id)
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


# === Guilds admin page ===

@app.get("/guilds", response_class=HTMLResponse)
async def guilds_page(request: Request, _user: dict = Depends(require_admin)):
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
    _user: dict = Depends(require_admin),
):
    """Одобрить сервер."""
    import guild as guild_module
    await guild_module.approve_guild(guild_id, _user.get("discord_id", 0))
    return RedirectResponse(url="/guilds?saved=1", status_code=303)


@app.post("/guilds/{guild_id}/reject")
async def reject_guild_endpoint(
    guild_id: int,
    _user: dict = Depends(require_admin),
):
    """Отклонить сервер."""
    import guild as guild_module
    await guild_module.reject_guild(guild_id)
    return RedirectResponse(url="/guilds?saved=1", status_code=303)


@app.post("/guilds/{guild_id}/delete")
async def delete_guild_endpoint(
    guild_id: int,
    _user: dict = Depends(require_admin),
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
    _user: dict = Depends(require_admin),
    make_admin: bool = Form(False),
):
    await db.set_admin(discord_id, make_admin)
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
    has_more = (offset + per_page) < total
    return templates.TemplateResponse(request, "quotes.html", {
        "user": _user,
        "quotes": quotes,
        "search": q,
        "page": page,
        "has_more": has_more,
        "total": total,
        "is_admin": _user.get("is_admin", False),
        "current_discord_id": _user.get("discord_id", 0),
    })


@app.post("/api/quotes/import-from-discord")
async def api_import_quotes_from_discord(_user: dict = Depends(require_user)):
    """Импортировать существующие сообщения из Discord-канала #цитатник в БД.

    Логика:
    - Читаем последние 100 сообщений из channel_quotes_id
    - Пропускаем сообщения от ботов (чтобы не было цикла)
    - Для каждого сообщения:
      - author = "Неизвестен"
      - text = содержимое сообщения
      - recorded_by = ID автора сообщения
      - message_link = ссылка на сообщение
    - Пропускаем дубликаты (по message_link)
    """
    import discord as discord_lib

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

    # Читаем последние 100 сообщений
    imported = 0
    skipped_bot = 0
    skipped_duplicate = 0

    try:
        async for message in channel.history(limit=100, oldest_first=True):
            # Пропускаем сообщения ботов (чтобы не было цикла самоповтора)
            if message.author.bot:
                skipped_bot += 1
                continue

            text = message.content.strip()
            if not text:
                continue  # пустое сообщение (например, только embed)

            # Ссылка на сообщение
            message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

            # Проверяем дубликат по message_link
            existing_quotes = await db.g_list_quotes(guild_id, limit=500, search=None)
            is_duplicate = any(
                q[7] == message_link for q in existing_quotes  # q[7] = message_link
            ) if existing_quotes else False

            if is_duplicate:
                skipped_duplicate += 1
                continue

            # Создаём цитату
            await db.g_add_quote(
                guild_id,
                author="Неизвестен",
                author_user_id=message.author.id,
                text=text,
                recorded_by=message.author.id,
                author_avatar_url=str(message.author.display_avatar.url) if message.author.display_avatar else None,
                message_link=message_link,
            )
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
    return JSONResponse({"ok": True, "id": quote_id})


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
    tg_link = await db.get_tg_link(discord_id) if discord_id else None
    return templates.TemplateResponse(request, "profile.html", {
        "user": _user,
        "tg_link": tg_link,
    })


# === Wheel (собственное колесо в панели) ===

@app.get("/wheel", response_class=HTMLResponse)
async def wheel_page(request: Request, _user: dict = Depends(require_user)):
    """Страница с Canvas-анимацией колеса."""
    guild_id = get_current_guild_id(_user)
    items = await db.g_list_wheel_items(guild_id, active_only=True)

    # Статус filmnight
    import guild as guild_module
    await guild_module.init_guild_tables(guild_id)
    filmnight = await db.g_get_active_filmnight(guild_id)

    return templates.TemplateResponse(request, "wheel.html", {
        "user": _user,
        "items": items,
        "current_guild_id": guild_id,
        "filmnight": filmnight,
    })


@app.get("/api/wheel/items")
async def api_wheel_items(_user: dict = Depends(require_user)):
    """Получить текущие лоты колеса (JSON)."""
    guild_id = get_current_guild_id(_user)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    return JSONResponse({"items": items, "count": len(items)})


# === FilmNight API (управление сбором фильмов из веб-панели) ===

@app.get("/api/filmnight/status")
async def api_filmnight_status(_user: dict = Depends(require_user)):
    """Статус активного сбора фильмов."""
    guild_id = get_current_guild_id(_user)
    import guild as guild_module
    await guild_module.init_guild_tables(guild_id)
    fn = await db.g_get_active_filmnight(guild_id)
    if not fn:
        return JSONResponse({"active": False})
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    unique_users = len(set(item["added_by"] for item in items)) if items else 0
    return JSONResponse({
        "active": True,
        "id": fn["id"],
        "max_per_user": fn["max_per_user"],
        "started_by": fn["started_by"],
        "started_at": fn["started_at"],
        "items_count": len(items),
        "unique_users": unique_users,
    })


@app.post("/api/filmnight/start")
async def api_filmnight_start(
    _user: dict = Depends(require_user),
    max_per_user: int = Form(3),
):
    """Запустить сбор фильмов из веб-панели + пост в Discord."""
    guild_id = get_current_guild_id(_user)
    import guild as guild_module
    await guild_module.init_guild_tables(guild_id)
    user_discord_id = _user.get("discord_id", 0)
    fn_id = await db.g_start_filmnight(guild_id, user_discord_id, max_per_user)

    # Постим в Discord-канал анонсов (если настроен)
    announce_channel_id_str = await db.get_setting("channel_announce_id")
    if announce_channel_id_str and announce_channel_id_str.isdigit():
        try:
            import bot as bot_module
            bot_instance = bot_module.get_bot_instance()
            if bot_instance:
                channel = bot_instance.get_channel(int(announce_channel_id_str))
                if channel:
                    import discord
                    embed = discord.Embed(
                        title="🎬 Сбор фильмов начат!",
                        description=(
                            f"Лимит: **{max_per_user}** фильмов на участника\n\n"
                            f"Используйте `/wheel add <название>` чтобы предложить фильм.\n"
                            f"Крутить: веб-панель → /wheel"
                        ),
                        color=0x2ECC71,
                    )
                    await channel.send(embed=embed)
        except Exception as e:
            import logging
            logging.getLogger("web").warning("Discord filmnight announce failed: %s", e)

    return JSONResponse({"ok": True, "id": fn_id, "max_per_user": max_per_user})


@app.post("/api/filmnight/end")
async def api_filmnight_end(_user: dict = Depends(require_user)):
    """Завершить сбор фильмов из веб-панели + пост в Discord."""
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)

    # Статистика перед завершением
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    unique_users = len(set(item["added_by"] for item in items)) if items else 0

    completed = await db.g_complete_filmnight(guild_id, user_discord_id)

    # Постим в Discord-канал анонсов
    announce_channel_id_str = await db.get_setting("channel_announce_id")
    if announce_channel_id_str and announce_channel_id_str.isdigit():
        try:
            import bot as bot_module
            bot_instance = bot_module.get_bot_instance()
            if bot_instance:
                channel = bot_instance.get_channel(int(announce_channel_id_str))
                if channel:
                    import discord
                    embed = discord.Embed(
                        title="✅ Сбор фильмов завершён!",
                        description=(
                            f"Собрано: **{len(items)}** фильмов от **{unique_users}** участник(ов)\n\n"
                            f"Крутить колесо: веб-панель → /wheel"
                        ),
                        color=0xFFB703,
                    )
                    await channel.send(embed=embed)
        except Exception as e:
            import logging
            logging.getLogger("web").warning("Discord filmnight end announce failed: %s", e)

    return JSONResponse({"ok": completed})


# === Wheel API ===

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
    guild_id = get_current_guild_id(_user)
    discord_id = _user.get("discord_id", 0)
    item_id = await db.g_add_wheel_item(guild_id, name, tmdb_id, discord_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(items)
    return JSONResponse({"id": item_id, "items": items, "count": len(items)})


@app.delete("/api/wheel/items/{item_id}")
async def api_remove_wheel_item(
    item_id: int,
    _user: dict = Depends(require_user),
):
    """Удалить лот (soft-delete). Триггерит broadcast."""
    guild_id = get_current_guild_id(_user)
    removed = await db.g_remove_wheel_item(guild_id, item_id)
    if not removed:
        return JSONResponse({"error": "not found"}, status_code=404)
    await db.g_reassign_colors(guild_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(items)
    return JSONResponse({"removed": True, "items": items, "count": len(items)})


@app.post("/api/wheel/clear")
async def api_clear_wheel(_user: dict = Depends(require_admin)):
    """Очистить всё колесо (только админ)."""
    guild_id = get_current_guild_id(_user)
    count = await db.g_clear_wheel(guild_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(items)
    return JSONResponse({"cleared": count, "items": items})


@app.post("/api/wheel/spin")
async def api_spin_wheel(_user: dict = Depends(require_user)):
    """Запустить спин. Сервер выбирает победителя и рассылает результат через WS.
    Также завершает активный filmnight (если есть).
    """
    import random
    guild_id = get_current_guild_id(_user)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    if len(items) < 2:
        return JSONResponse({"error": "need at least 2 items to spin"}, status_code=400)

    # Завершаем активный сбор фильмов (если есть)
    await db.g_complete_filmnight(guild_id, _user.get("discord_id", 0))

    # Случайный победитель
    winner = random.choice(items)
    spin_id = str(uuid.uuid4())[:8]

    # Рассылаем событие спина (клиенты начинают анимацию)
    await ws_manager.broadcast_spin_started(items, spin_id)

    # Записываем в БД как confirmed (мы точно знаем победителя)
    await db.g_add_winner(guild_id, str(winner["id"]), winner["name"], winner.get("tmdb_id"), "confirmed")

    # Удаляем победителя из колеса
    await db.g_remove_wheel_item(guild_id, winner["id"])
    await db.g_reassign_colors(guild_id)

    # Финальный список после удаления победителя
    remaining = await db.g_list_wheel_items(guild_id, active_only=True)

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
    """Режим 'на выбывание'."""
    import random
    guild_id = get_current_guild_id(_user)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    if len(items) < 2:
        return JSONResponse({"error": "need at least 2 items"}, status_code=400)

    # Завершаем активный сбор фильмов при первом elimination-спине
    await db.g_complete_filmnight(guild_id, _user.get("discord_id", 0))

    spin_id = str(uuid.uuid4())[:8]

    # Если ровно 2 — финальный раунд, победитель = тот кто ОСТАЛСЯ
    if len(items) == 2:
        # Случайно удаляем одного — оставшийся победитель
        eliminated = random.choice(items)
        winner = next(it for it in items if it["id"] != eliminated["id"])

        await db.g_remove_wheel_item(guild_id, eliminated["id"])
        await db.g_remove_wheel_item(guild_id, winner["id"])
        await db.g_reassign_colors(guild_id)

        # Записываем победителя в winners
        await db.g_add_winner(guild_id, str(winner["id"]), winner["name"], winner.get("tmdb_id"), "confirmed")

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
    await db.g_remove_wheel_item(guild_id, eliminated["id"])
    await db.g_reassign_colors(guild_id)
    remaining = await db.g_list_wheel_items(guild_id, active_only=True)

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
    """WebSocket для подписки на обновления колеса.
    guild_id передаётся как query-параметр: /ws/wheel?guild_id=123
    """
    import json
    # Читаем guild_id из query-параметров (по умолчанию 0)
    guild_id_str = websocket.query_params.get("guild_id", "0")
    try:
        guild_id = int(guild_id_str)
    except (ValueError, TypeError):
        guild_id = 0

    await ws_manager.connect(websocket)
    try:
        # При первом подключении сразу шлём текущее состояние для этого guild
        items = await db.g_list_wheel_items(guild_id, active_only=True)
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
