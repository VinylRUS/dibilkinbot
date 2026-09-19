"""FastAPI веб-панель: логин, токены, каналы, тогглы фич."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import crypto, db
from .config import settings

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 дней

serializer = URLSafeTimedSerializer(settings.app_secret, salt="panel-session")

app = FastAPI(title="Kinovecher Panel", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent.parent / "static")), name="static")


# === Session ===

def create_session(user: str) -> str:
    return serializer.dumps({"u": user, "t": datetime.utcnow().isoformat()})


def read_session(token: str) -> Optional[str]:
    try:
        data = serializer.loads(token, max_age=SESSION_TTL)
        return data.get("u")
    except BadSignature:
        return None


def get_current_user(request: Request) -> str:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user = read_session(token)
    if not user:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


async def admin_creds() -> tuple[str, str]:
    """Логин/пароль админа. Можно перезаписать в settings-таблице БД через панель."""
    login = await db.get_setting("admin_login") or settings.admin_login
    password = await db.get_setting("admin_password") or settings.admin_password
    return login, password


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
    password: str = Form(...),
):
    expected_login, expected_password = await admin_creds()
    # Constant-time compare
    ok_login = secrets.compare_digest(login, expected_login)
    ok_password = secrets.compare_digest(password, expected_password)
    if not (ok_login and ok_password):
        return RedirectResponse(url="/login?error=invalid", status_code=303)

    token = create_session(login)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        "session", token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=False,  # на bothost.tech будет HTTPS через Traefik, но куку ставим без secure чтобы работало и на http
    )
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session")
    return resp


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _user: str = Depends(get_current_user)):
    # Текущие настройки для отображения (маскируем секреты)
    raw_settings = await db.list_settings()
    shown = []
    for key, value, is_secret in raw_settings:
        if is_secret:
            shown.append((key, "✓ сохранён" if value else "—", True, bool(value)))
        else:
            shown.append((key, value or "—", False, bool(value)))

    # Список просмотренных (последние 10) и топ цитат
    recent_watched = await db.list_watched(limit=10)
    top_quotes = await db.top_quotes(limit=5)

    return templates.TemplateResponse(request, "panel.html", {
        "user": _user,
        "settings": shown,
        "recent_watched": recent_watched,
        "top_quotes": top_quotes,
    })


# === Tokens ===

KNOWN_TOKENS = [
    ("discord_token", "Discord Bot Token"),
    ("telegram_token", "Telegram Bot Token"),
    ("pointauc_token", "Pointauc Personal Token"),
]


@app.get("/tokens", response_class=HTMLResponse)
async def tokens_form(request: Request, _user: str = Depends(get_current_user)):
    # Какие уже сохранены
    saved = {}
    for key, _label in KNOWN_TOKENS:
        v = await db.get_setting(key)
        saved[key] = bool(v)
    return templates.TemplateResponse(request, "tokens.html", {
        "tokens": KNOWN_TOKENS,
        "saved": saved,
    })


@app.post("/tokens")
async def tokens_save(
    request: Request,
    _user: str = Depends(get_current_user),
    discord_token: str = Form(""),
    telegram_token: str = Form(""),
    pointauc_token: str = Form(""),
):
    updates = {
        "discord_token": discord_token.strip(),
        "telegram_token": telegram_token.strip(),
        "pointauc_token": pointauc_token.strip(),
    }
    for key, val in updates.items():
        if val:
            encrypted = crypto.encrypt(val)
            await db.set_setting(key, encrypted, is_secret=True)
        else:
            # пустой ввод — не трогаем существующее
            pass
    return RedirectResponse(url="/tokens?saved=1", status_code=303)


# === Channels ===

KNOWN_CHANNELS = [
    ("channel_quotes_id", "ID канала #цитатник (куда постить embed цитат)"),
    ("channel_announce_id", "ID канала анонсов киновечера"),
    ("role_movie_ping_id", "ID роли для пинга анонсов киновечера"),
    ("telegram_chat_id", "Telegram chat_id для бэклога (число, не @username)"),
]


@app.get("/channels", response_class=HTMLResponse)
async def channels_form(request: Request, _user: str = Depends(get_current_user)):
    values = {key: (await db.get_setting(key) or "") for key, _label in KNOWN_CHANNELS}
    return templates.TemplateResponse(request, "channels.html", {
        "channels": KNOWN_CHANNELS,
        "values": values,
    })


@app.post("/channels")
async def channels_save(
    request: Request,
    _user: str = Depends(get_current_user),
    channel_quotes_id: str = Form(""),
    channel_announce_id: str = Form(""),
    role_movie_ping_id: str = Form(""),
    telegram_chat_id: str = Form(""),
):
    updates = {
        "channel_quotes_id": channel_quotes_id.strip(),
        "channel_announce_id": channel_announce_id.strip(),
        "role_movie_ping_id": role_movie_ping_id.strip(),
        "telegram_chat_id": telegram_chat_id.strip(),
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
async def features_form(request: Request, _user: str = Depends(get_current_user)):
    values = {key: (await db.get_setting(key) or default) for key, _label, default in KNOWN_FEATURES}
    return templates.TemplateResponse(request, "features.html", {
        "features": KNOWN_FEATURES,
        "values": values,
    })


@app.post("/features")
async def features_save(
    request: Request,
    _user: str = Depends(get_current_user),
    tg_crosspost_quotes: bool = Form(False),
    tg_crosspost_announce: bool = Form(False),
):
    await db.set_setting("tg_crosspost_quotes", "1" if tg_crosspost_quotes else "0", is_secret=False)
    await db.set_setting("tg_crosspost_announce", "1" if tg_crosspost_announce else "0", is_secret=False)
    return RedirectResponse(url="/features?saved=1", status_code=303)
