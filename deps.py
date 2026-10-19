"""Общие зависимости для веб-панели: auth, templates, утилиты.

Импортируется web.py, wheel_routes.py, movienight_routes.py — без circular imports.
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

import crypto
import db
from config import settings

# === Paths ===
TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_TTL = 60 * 60 * 24 * 7  # 7 дней

# === Session ===
serializer = URLSafeTimedSerializer(settings.app_secret, salt="panel-session-v3")


def create_session(payload: dict) -> str:
    return serializer.dumps({"u": payload, "t": datetime.utcnow().isoformat()})


def read_session(token: str) -> dict | None:
    """Возвращает payload (dict) из сессии или None."""
    try:
        data = serializer.loads(token, max_age=SESSION_TTL)
        user = data.get("u") if isinstance(data, dict) else None
        if isinstance(user, dict):
            return user
        return None
    except BadSignature:
        return None


# === Jinja2 Templates ===
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _get_git_commit_id() -> str:
    """Короткий идентификатор сборки (7 символов)."""
    if hasattr(_get_git_commit_id, '_cached'):
        return _get_git_commit_id._cached
    commit = 'unknown'
    env_commit = os.environ.get('GIT_COMMIT', '').strip()
    if env_commit:
        commit = env_commit[:7]
    if commit == 'unknown':
        git_commit_file = Path(__file__).parent / '.git_commit'
        if git_commit_file.exists():
            try:
                file_commit = git_commit_file.read_text().strip()
                if file_commit:
                    commit = file_commit[:7]
            except Exception:
                pass
    if commit == 'unknown':
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'],
                capture_output=True, text=True, timeout=2.0,
                cwd=str(Path(__file__).parent),
            )
            if result.returncode == 0 and result.stdout.strip():
                commit = result.stdout.strip()[:7]
        except Exception:
            pass
    if commit == 'unknown':
        try:
            mtime = Path(__file__).stat().st_mtime
            commit = datetime.fromtimestamp(mtime).strftime('%Y%m%d')
        except Exception:
            pass
    _get_git_commit_id._cached = commit
    return commit


def _get_bot_version() -> str:
    """Текущая версия из CHANGELOG. Кешируется."""
    if hasattr(_get_bot_version, '_cached'):
        return _get_bot_version._cached
    from changelog_parser import get_latest_version
    _get_bot_version._cached = get_latest_version("CHANGELOG.md")
    return _get_bot_version._cached


def _setup_templates_globals():
    """Регистрирует глобальные функции в Jinja2 — вызывается один раз."""
    templates.env.globals['bot_version'] = _get_bot_version()
    templates.env.globals['commit_id'] = _get_git_commit_id()
    templates.env.globals['bot_version_with_commit'] = f"{_get_bot_version()}({_get_git_commit_id()})"
    templates.env.globals["theme_from_request"] = lambda request: request.cookies.get("theme", "light")
    templates.env.globals["format_msk"] = None  # set by web.py after import
    templates.env.globals["format_msk_short"] = None
    templates.env.globals["santa_enabled"] = None


# === Auth dependencies ===

async def get_current_user(request: Request) -> dict | None:
    """Возвращает user dict из сессии, или None."""
    token = request.cookies.get("session")
    if not token:
        return None
    user = read_session(token)
    if not user:
        return None
    discord_id = user.get("discord_id")
    guild_id = user.get("current_guild_id", 0)
    if discord_id and guild_id:
        try:
            user["unread_achievements_count"] = await db.g_count_unread_achievements(guild_id, discord_id)
        except Exception:
            user["unread_achievements_count"] = 0
    else:
        user["unread_achievements_count"] = 0
    return user


async def require_user(request: Request) -> dict:
    """Зависимость: юзер обязан быть залогинен."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


async def require_admin(request: Request) -> dict:
    """Зависимость: юзер обязан быть админом."""
    user = await require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Доступ только для администратора")
    return user


async def require_superuser(request: Request) -> dict:
    """Зависимость: юзер обязан быть superuser (Матка)."""
    user = await require_user(request)
    if not user.get("is_superuser"):
        raise HTTPException(status_code=403, detail="Доступ только для суперпользователя")
    return user


# === Utilities ===

def get_current_guild_id(user_payload: dict) -> int:
    """Текущий выбранный сервер из сессии. По умолчанию 0."""
    return user_payload.get("current_guild_id", 0)


def anonymize_items(items: list[dict]) -> list[dict]:
    """Убрать added_by и added_at из списка лотов — анонимность."""
    return [
        {"id": item["id"], "name": item["name"], "tmdb_id": item["tmdb_id"], "color": item["color"]}
        for item in items
    ]
