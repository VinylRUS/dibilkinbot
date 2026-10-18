"""
Точка входа: один процесс — FastAPI (uvicorn) + discord.py + TG long-polling в одном asyncio loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import httpx
import uvicorn

import crypto
import db
import log_buffer
import telegram
from bot import KinovecherBot, get_token, handle_tg_link_message
from config import settings
from web import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Глушим спам от httpx INFO логов (TG getUpdates каждый раз пишет "HTTP Request: POST ...")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("main")

# v2.4.3: In-memory ring buffer для команды /logs в TG.
# Хранит логи за 24ч, доступ через команду /logs (только Матка).
log_buffer.install()

TG_API = "https://api.telegram.org/bot{token}/{method}"


async def run_bot() -> KinovecherBot | None:
    token = await get_token("discord_token", settings.discord_token)
    if not token:
        log.warning("DISCORD_TOKEN не задан. Бот не запущен. Веб-панель доступна на :%s", settings.port)
        log.warning("Задайте токен через веб-панель /tokens и перезапустите процесс.")
        return None
    bot = KinovecherBot()
    task = asyncio.create_task(bot.start(token))
    def _check(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error("Discord bot crashed: %r", exc)
    task.add_done_callback(_check)
    return bot


async def handle_logs_command(text: str, tg_user_id: int, tg_username: str | None) -> str | None:
    """Обработать команду /logs из Telegram. Только Матка.

    Формат:
      /logs — последние 24 часа, все уровни (INFO+), лимит 500 записей
      /logs warn — последние 24 часа, только WARNING+
      /logs error — последние 24 часа, только ERROR+
      /logs 1h — последний час, все уровни
      /logs 6h warn — последние 6 часов, WARNING+
      /logs stats — статистика буфера

    Доступ: только если tg_user_id привязан к admin_discord_id в Discord.
    """
    import logging as logging_mod
    import log_buffer as lb

    # Проверка доступа — пользователь должен быть привязан как Матка
    admin_discord_id = settings.admin_discord_id
    if not admin_discord_id:
        return "❌ Команда доступна только администратору. ADMIN_DISCORD_ID не настроен."
    admin_tg = await db.get_tg_link(admin_discord_id)
    if not admin_tg or not admin_tg[1]:
        return ("❌ Команда доступна только Матке. "
                "Сначала привяжите Telegram через /linktg в Discord.")
    admin_tg_id = admin_tg[1]
    if str(tg_user_id) != str(admin_tg_id):
        # Не Матка — игнорируем, не сообщаем что команда существует
        return None

    # Парсим аргументы
    parts = text.split()
    args = parts[1:] if len(parts) > 1 else []

    hours = 24.0
    level = None  # INFO+
    show_stats = False

    for arg in args:
        arg_lower = arg.lower()
        # Часы: 1h, 6h, 12h, 24h
        if arg_lower.endswith("h") and arg_lower[:-1].isdigit():
            hours = min(float(arg_lower[:-1]), 24.0)
        # Уровень
        elif arg_lower in ("debug", "info", "warn", "warning", "error", "critical"):
            level_map = {
                "debug": logging_mod.DEBUG,
                "info": logging_mod.INFO,
                "warn": logging_mod.WARNING,
                "warning": logging_mod.WARNING,
                "error": logging_mod.ERROR,
                "critical": logging_mod.CRITICAL,
            }
            level = level_map[arg_lower]
        elif arg_lower == "stats":
            show_stats = True
        else:
            return (f"❓ Неизвестный аргумент: <code>{arg}</code>\n\n"
                    "<b>Использование:</b>\n"
                    "<code>/logs</code> — последние 24ч\n"
                    "<code>/logs warn</code> — только WARNING+\n"
                    "<code>/logs error</code> — только ERROR+\n"
                    "<code>/logs 1h</code> — последний час\n"
                    "<code>/logs 6h warn</code> — 6ч, WARNING+\n"
                    "<code>/logs stats</code> — статистика буфера")

    handler = lb.get_memory_handler()

    if show_stats:
        stats = handler.get_stats()
        by_level = stats["by_level"]
        lines = [
            "📊 <b>Статистика лог-буфера</b>",
            "",
            f"Всего записей: <b>{stats['total']}</b>",
            f"Самая старая: <b>{stats['oldest_age_seconds'] // 60} мин назад</b>",
            f"Retention: <b>{int(stats['retention_hours'])}ч</b>",
            "",
            "<b>По уровням:</b>",
            f"  INFO: {by_level.get('INFO', 0)}",
            f"  WARNING: {by_level.get('WARNING', 0)}",
            f"  ERROR: {by_level.get('ERROR', 0)}",
            f"  CRITICAL: {by_level.get('CRITICAL', 0)}",
        ]
        return "\n".join(lines)

    entries = handler.get_recent(hours=hours, level=level, limit=500)
    level_str = f" ({_level_name(level)}+)" if level else " (INFO+)"
    header = (f"📋 <b>Логи за последние {hours:.0f}ч{level_str}</b>\n"
              f"Записей: {len(entries)}\n\n")
    body = lb.format_logs_for_tg(entries, max_chars=3500 - len(header))
    return header + body


def _level_name(level: int) -> str:
    """Имя уровня логирования."""
    import logging as logging_mod
    return logging_mod.getLevelName(level)


async def run_tg_long_polling() -> asyncio.Task | None:
    """Long-polling getUpdates для приёма кодов привязки. Молча отключается если TG не настроен."""
    async def _tg_loop():
        last_update_id = 0
        while True:
            try:
                # Получаем TG-токен: из БД (если задан через панель) или из env
                raw = await db.get_setting("telegram_token")
                tg_token = crypto.decrypt(raw) if raw else settings.telegram_token
                if not tg_token:
                    await asyncio.sleep(60)
                    continue

                url = TG_API.format(token=tg_token, method="getUpdates")
                payload = {"timeout": 30, "offset": last_update_id + 1}
                try:
                    async with httpx.AsyncClient(timeout=35) as client:
                        r = await client.post(url, json=payload)
                    if r.status_code >= 400:
                        log.warning("TG getUpdates error %s: %s", r.status_code, r.text[:200])
                        await asyncio.sleep(10)
                        continue
                    data = r.json()
                except Exception as e:
                    log.warning("TG getUpdates failed: %s", e)
                    await asyncio.sleep(10)
                    continue

                for update in data.get("result", []):
                    last_update_id = update.get("update_id", last_update_id)
                    msg = update.get("message") or update.get("channel_post")
                    if not msg:
                        continue
                    text = msg.get("text", "").strip()
                    tg_user = msg.get("from", {})
                    tg_user_id = tg_user.get("id")
                    tg_username = tg_user.get("username")
                    if not text or not tg_user_id:
                        continue

                    # v2.4.3: команда /logs (только Матка)
                    if text.startswith("/logs"):
                        reply = await handle_logs_command(text, tg_user_id, tg_username)
                        if reply:
                            await telegram.send_message(tg_token, str(msg["chat"]["id"]), reply)
                        continue

                    reply = await handle_tg_link_message(text, tg_user_id, tg_username)
                    if reply:
                        await telegram.send_message(tg_token, str(msg["chat"]["id"]), reply)
                    else:
                        # Нераспознанное сообщение — отвечаем подсказкой
                        await telegram.send_message(
                            tg_token, str(msg["chat"]["id"]),
                            "Привет! Я бот киновечеров. Чтобы привязать аккаунт, "
                            "отправьте код из команды /linktg в Discord."
                        )

            except asyncio.CancelledError:
                log.info("TG long-polling cancelled")
                return
            except Exception as e:
                log.error("TG loop unexpected error: %s", e, exc_info=True)
                await asyncio.sleep(15)

    return asyncio.create_task(_tg_loop())


async def main() -> None:
    await db.init_db()
    log.info("=" * 60)
    log.info("DATABASE PATH: %s", settings.database_path)
    log.info("DATABASE exists: %s", settings.database_path.exists())
    if settings.database_path.exists():
        log.info("DATABASE size: %d bytes", settings.database_path.stat().st_size)
    log.info("=" * 60)

    await run_bot()
    tg_task = await run_tg_long_polling()
    if tg_task:
        log.info("TG long-polling started")

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    log.info("Web panel starting on http://%s:%s", settings.host, settings.port)
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
