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
