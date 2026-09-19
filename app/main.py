"""
Точка входа. Один процесс: FastAPI (uvicorn) + discord.py бот в одном asyncio loop.

Поведение при отсутствии токена Discord:
  - Веб-панель всё равно стартует (можно через неё прописать токен).
  - Бот не запускается, в логах предупреждение.

Аналогично для Pointauc / Telegram — они ленивые, проверяются при первой команде.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from . import crypto, db
from .bot import KinovecherBot, get_token
from .config import settings
from .web import app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")


async def run_bot() -> KinovecherBot | None:
    """Запускает бота как фоновую задачу. Возвращает None если токена нет."""
    token = await get_token("discord_token", settings.discord_token)
    if not token:
        log.warning("DISCORD_TOKEN не задан. Бот не запущен. Веб-панель доступна на :%s", settings.port)
        log.warning("Задайте токен через веб-панель /tokens и перезапустите процесс.")
        return None

    bot = KinovecherBot()
    task = asyncio.create_task(bot.start(token))

    # Не даём task'у молча умереть
    def _check(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error("Discord bot crashed: %r", exc)
    task.add_done_callback(_check)
    return bot


async def main() -> None:
    # 1. Инициализируем БД (синхронно относительно старта бота — чтобы токены читались корректно)
    await db.init_db()
    log.info("DB initialized at %s", settings.database_path)

    # 2. Запускаем бота в фоне (если есть токен)
    await run_bot()

    # 3. Запускаем FastAPI через uvicorn (блокирует основной loop до SIGTERM)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,  # чтобы не засорять лог, у нас свой логгер
    )
    server = uvicorn.Server(config)
    log.info("Web panel starting on http://%s:%s", settings.host, settings.port)
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
