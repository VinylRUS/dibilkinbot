"""v2.3.3: GitHub мониторинг — polling новых коммитов + presence status.

Раз в 10 минут опрашивает GitHub API:
GET https://api.github.com/repos/VinylRUS/dibilkinbot/commits/main

Сравнивает hash из ответа с локальным .git_commit.
Если отличаются — бот отстаёт от main, нужно redeploy.

GitHub PAT (опционально, для снятия лимита 60→5000 req/hour):
- Хранится в settings (github_token) или БД (settings table)
- Без токена: 60 req/hour (нам нужно 6/hour — хватает)
- С токеном: 5000 req/hour

Уведомления:
- Discord presence: "Playing v2.3.3(354073b)" или "Playing ⚠️ v2.3.3 — есть обновление!"
- TG Матке: если отставание обнаружено впервые (не спамим каждый цикл)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx

import db
from config import settings

log = logging.getLogger("github_monitor")

REPO_OWNER = "VinylRUS"
REPO_NAME = "dibilkinbot"
GITHUB_API = "https://api.github.com"
POLL_INTERVAL_WITH_TOKEN = 10   # 10 сек — с PAT токеном (лимит 5000/hour)
POLL_INTERVAL_NO_TOKEN = 600    # 10 минут — без токена (лимит 60/hour)
NOTIFY_COOLDOWN = 3600  # 1 час — не спамим Матку чаще

# Состояние: последний известный hash с GitHub + время последнего уведомления
_last_github_hash: str | None = None
_last_notify_time: float = 0.0
_last_was_behind: bool = False


def _get_local_commit() -> str:
    """Локальный hash из .git_commit файла (или git rev-parse)."""
    # 1. ENV GIT_COMMIT
    env_commit = os.environ.get("GIT_COMMIT", "").strip()
    if env_commit:
        return env_commit[:7]

    # 2. Файл .git_commit (создаётся в Dockerfile)
    git_commit_file = Path(__file__).parent / ".git_commit"
    if git_commit_file.exists():
        try:
            content = git_commit_file.read_text().strip()
            if content and content != "unknown":
                return content[:7]
        except Exception:
            pass

    # 3. git rev-parse (локальная разработка)
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2.0,
            cwd=str(Path(__file__).parent),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()[:7]
    except Exception:
        pass

    return "unknown"


async def _get_github_token() -> str | None:
    """GitHub PAT из БД или env. Опциональный — без него лимит 60/hour."""
    raw = await db.get_setting("github_token")
    if raw:
        try:
            from crypto import decrypt
            return decrypt(raw)
        except Exception:
            return None
    return getattr(settings, "github_token", None) or None


async def fetch_latest_commit() -> str | None:
    """Получить hash последнего коммита на GitHub main.

    Возвращает короткий hash (7 символов) или None при ошибке.
    """
    url = f"{GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/commits/main"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "DeeBeelkin-Bot/2.3",
    }
    token = await _get_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=headers)

        if r.status_code == 200:
            data = r.json()
            sha = data.get("sha", "")
            if sha:
                return sha[:7]
        elif r.status_code == 403:
            # Rate limit
            remaining = r.headers.get("X-RateLimit-Remaining", "?")
            log.warning("GitHub API rate limit (remaining: %s)", remaining)
        elif r.status_code == 404:
            log.warning("GitHub API: repo or branch not found")
        else:
            log.warning("GitHub API HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("GitHub API request failed: %s", e)

    return None


async def _send_tg_to_admin(text: str) -> None:
    """Отправить сообщение Матке в Telegram."""
    try:
        import crypto as crypto_mod
        import telegram
        admin_id = settings.admin_discord_id
        if not admin_id:
            return
        tg_link = await db.get_tg_link(admin_id)
        if not tg_link or not tg_link[1]:
            return
        tg_user_id = str(tg_link[1])
        raw_token = await db.get_setting("telegram_token")
        if not raw_token:
            return
        tg_token = crypto_mod.decrypt(raw_token)
        await telegram.send_message(tg_token, tg_user_id, text)
    except Exception as e:
        log.warning("Failed to send TG to admin: %s", e)


async def _update_discord_presence(is_behind: bool, local_hash: str, remote_hash: str | None) -> None:
    """Обновить presence status бота в Discord."""
    try:
        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if not bot:
            return

        from deps import _get_bot_version
        version = _get_bot_version()

        if is_behind:
            activity_text = f"⚠️ {version} — есть обновление!"
        else:
            activity_text = f"{version}({local_hash})"

        import discord
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name=activity_text,
            )
        )
        log.info("Discord presence updated: %s", activity_text)
    except Exception as e:
        log.warning("Failed to update Discord presence: %s", e)


async def github_monitor_loop() -> None:
    """Фоновый цикл: проверяет GitHub на новые коммиты.

    Интервал зависит от наличия PAT токена:
    - С токеном: каждые 10 секунд (лимит 5000/hour, используем 360/hour)
    - Без токена: каждые 10 минут (лимит 60/hour, используем 6/hour)

    Логика:
    1. Получаем локальный hash
    2. Получаем hash с GitHub
    3. Если отличаются → бот отстаёт
       - Меняем presence на ⚠️
       - Шлём TG Матке (если не спамили в последний час)
    4. Если совпадают → бот актуальный
       - Меняем presence на ✓
    """
    global _last_github_hash, _last_notify_time, _last_was_behind

    # Определяем интервал по наличию токена
    token = await _get_github_token()
    interval = POLL_INTERVAL_WITH_TOKEN if token else POLL_INTERVAL_NO_TOKEN
    log.info("GitHub monitor started (interval=%ds, token=%s)", interval, "yes" if token else "no")

    # Первая проверка через 10 сек после старта
    await asyncio.sleep(10)

    while True:
        try:
            local_hash = _get_local_commit()
            remote_hash = await fetch_latest_commit()

            if remote_hash is None:
                # GitHub недоступен — не меняем статус, пробуем в следующий раз
                log.debug("GitHub API unavailable, skipping check")
            elif local_hash == "unknown":
                log.debug("Local commit unknown, skipping comparison")
            elif local_hash == remote_hash:
                # Версии совпадают — бот актуальный
                if _last_was_behind:
                    log.info("Bot is now up-to-date: %s == %s", local_hash, remote_hash)
                _last_was_behind = False
                await _update_discord_presence(False, local_hash, remote_hash)
            else:
                # Отставание!
                log.warning("Bot is BEHIND main! local=%s, remote=%s", local_hash, remote_hash)
                _last_was_behind = True
                await _update_discord_presence(True, local_hash, remote_hash)

                # TG Матке — если не спамили в последний час
                now = asyncio.get_event_loop().time()
                if now - _last_notify_time > NOTIFY_COOLDOWN:
                    _last_notify_time = now
                    await _send_tg_to_admin(
                        f"⚠️ <b>Бот отстаёт от main!</b>\n\n"
                        f"Локальный: <code>{local_hash}</code>\n"
                        f"GitHub: <code>{remote_hash}</code>\n\n"
                        f"Возможно нужен redeploy на BotHost."
                    )

            _last_github_hash = remote_hash

        except asyncio.CancelledError:
            log.info("GitHub monitor cancelled")
            return
        except Exception as e:
            log.error("GitHub monitor error: %s", e, exc_info=True)

        await asyncio.sleep(interval)


async def set_initial_presence() -> None:
    """Установить presence при старте бота (без ожидания polling-цикла)."""
    try:
        local_hash = _get_local_commit()
        from deps import _get_bot_version
        version = _get_bot_version()
        activity_text = f"{version}({local_hash})"

        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if bot:
            import discord
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name=activity_text,
                )
            )
            log.info("Initial Discord presence: %s", activity_text)
    except Exception as e:
        log.warning("Failed to set initial presence: %s", e)
