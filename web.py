"""FastAPI веб-панель: логин по Discord ID или логин/пароль, права is_admin."""
from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

import crypto
import db
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
    try:
        data = serializer.loads(token, max_age=SESSION_TTL)
        return data.get("u")
    except BadSignature:
        return None


async def get_current_user(request: Request) -> dict | None:
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


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form("", alias="password"),
):
    """Логин двумя способами:
    1. Логин = ADMIN_LOGIN из env, пароль = ADMIN_PASSWORD (env-админ)
    2. Логин = Discord ID (число), пароль пустой (только для юзеров в БД)
    """
    login = login.strip()
    password = password.strip()

    # Способ 1: env-админ
    is_env_admin_login = secrets.compare_digest(login, settings.admin_login)
    is_env_admin_pass = secrets.compare_digest(password, settings.admin_password)
    if is_env_admin_login and is_env_admin_pass:
        # Создаём/обновляем запись юзера в БД с is_admin=1 (если login — число = Discord ID)
        if login.isdigit():
            discord_id = int(login)
            await db.upsert_user(discord_id, username="admin", display_name="Admin")
            await db.set_admin(discord_id, True)
            user_payload = {"discord_id": discord_id, "username": "admin", "is_admin": True}
        else:
            # Логин не числовой — сессионный админ без записи в БД
            user_payload = {"discord_id": 0, "username": login, "is_admin": True}
        token = create_session(user_payload)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie("session", token, max_age=SESSION_TTL, httponly=True, samesite="lax")
        return resp

    # Способ 2: Discord ID (только если цифры и юзер в БД)
    if login.isdigit():
        discord_id = int(login)
        user_row = await db.get_user(discord_id)
        if user_row:
            # Обновляем last_login
            await db.upsert_user(discord_id, username=user_row[1], display_name=user_row[2])
            is_adm = await db.is_admin(discord_id)
            user_payload = {
                "discord_id": discord_id,
                "username": user_row[1] or str(discord_id),
                "is_admin": is_adm,
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
    saved_pointauc = bool(await db.get_setting("pointauc_token"))
    saved_kp = bool(await db.get_setting("kinopoisk_token"))
    saved_tg = bool(await db.get_setting("telegram_token"))

    return templates.TemplateResponse(request, "panel.html", {
        "user": _user,
        "recent_watched": recent_watched,
        "recent_winners": recent_winners,
        "activity": activity,
        "top_quotes": top_quotes,
        "saved_pointauc": saved_pointauc,
        "saved_kp": saved_kp,
        "saved_tg": saved_tg,
    })


# === Tokens (admin only) ===

KNOWN_TOKENS = [
    ("discord_token", "Discord Bot Token"),
    ("telegram_token", "Telegram Bot Token"),
    ("pointauc_token", "Pointauc Personal Token"),
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
    pointauc_token: str = Form(""),
    kinopoisk_token: str = Form(""),
):
    updates = {
        "discord_token": discord_token.strip(),
        "telegram_token": telegram_token.strip(),
        "pointauc_token": pointauc_token.strip(),
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
    winners = await db.list_winners(limit=50)
    return templates.TemplateResponse(request, "winners.html", {
        "user": _user,
        "winners": winners,
    })


# === Watched page (для всех залогиненных) ===

@app.get("/watched", response_class=HTMLResponse)
async def watched_page(request: Request, _user: dict = Depends(require_user)):
    watched = await db.list_watched(limit=100)
    return templates.TemplateResponse(request, "watched.html", {
        "user": _user,
        "watched": watched,
    })


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
