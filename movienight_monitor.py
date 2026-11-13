"""v3.11: Мониторинг киновечеров — watch-party countdown + авто-промпт оценок.

Фоновый цикл (раз в минуту) по всем approved-гильдиям:
  • countdown: когда наступает время старта киновечера — постит «3-2-1, начинаем!»
    в канал анонсов (channel_announce_id), один раз (флаг countdown_sent).
  • rate-prompt: через RATE_DELAY_HOURS после старта — постит просьбу оценить фильм
    на панели, один раз (флаг rate_prompt_sent).

Discord-рантайм: проверяется на живом боте. Все действия обёрнуты в try/except,
чтобы сбой не ронял цикл.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import db

log = logging.getLogger("movienight_monitor")

POLL_INTERVAL_SECONDS = 60
FIRST_CHECK_DELAY = 45
RATE_DELAY_HOURS = 3           # через сколько часов после старта просить оценку
RATE_FLOOR_HOURS = 30          # не трогаем киновечера старше этого (анти-спам при деплое)
COUNTDOWN_WINDOW_SECONDS = 180  # окно «только что наступил старт»


async def _announce_channel(guild_id: int):
    """Канал анонсов киновечеров для гильдии (channel_announce_id) или None."""
    try:
        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if not bot:
            return None
        cid = await db.get_setting("channel_announce_id")
        if not cid or not str(cid).isdigit():
            return None
        return bot.get_channel(int(cid))
    except Exception as e:
        log.debug("announce channel lookup failed: %s", e)
        return None


async def _check_guild(guild_id: int) -> None:
    now = datetime.utcnow()
    now_iso = now.isoformat()

    # 1) Countdown — старт наступил в последнем окне
    window_start = (now - timedelta(seconds=COUNTDOWN_WINDOW_SECONDS)).isoformat()
    try:
        due = await db.g_movie_nights_due_countdown(guild_id, now_iso, window_start)
    except Exception as e:
        log.debug("due_countdown failed g=%s: %s", guild_id, e)
        due = []
    for mn in due:
        channel = await _announce_channel(guild_id)
        try:
            if channel is not None:
                title = mn.get("title") or "Киновечер"
                await channel.send(f"🎬 **{title}** начинается! 3… 2… 1… поехали — включайте! 🍿")
            await db.g_mark_movie_night_flag(guild_id, mn["id"], "countdown_sent")
        except Exception as e:
            log.warning("countdown post failed g=%s mn=%s: %s", guild_id, mn.get("id"), e)

    # 2) Rate-prompt — прошло RATE_DELAY_HOURS
    cutoff = (now - timedelta(hours=RATE_DELAY_HOURS)).isoformat()
    floor = (now - timedelta(hours=RATE_FLOOR_HOURS)).isoformat()
    try:
        due_rate = await db.g_movie_nights_due_rate(guild_id, cutoff, floor)
    except Exception as e:
        log.debug("due_rate failed g=%s: %s", guild_id, e)
        due_rate = []
    for mn in due_rate:
        channel = await _announce_channel(guild_id)
        try:
            if channel is not None:
                title = mn.get("title") or "фильм"
                await channel.send(
                    f"⭐ Как вам «{title}»? Поставьте оценку на панели в разделе «Победители»."
                )
            await db.g_mark_movie_night_flag(guild_id, mn["id"], "rate_prompt_sent")
        except Exception as e:
            log.warning("rate prompt failed g=%s mn=%s: %s", guild_id, mn.get("id"), e)


async def check_all_guilds_once() -> None:
    try:
        guild_ids = await db.list_approved_guilds()
    except Exception as e:
        log.warning("list_approved_guilds failed: %s", e)
        return
    for guild_id in guild_ids:
        try:
            await _check_guild(guild_id)
        except Exception as e:
            log.error("movienight check failed for guild %s: %s", guild_id, e, exc_info=True)


async def movienight_monitor_loop() -> None:
    log.info("MovieNight monitor started (interval=%ds)", POLL_INTERVAL_SECONDS)
    await asyncio.sleep(FIRST_CHECK_DELAY)
    while True:
        try:
            await check_all_guilds_once()
        except Exception as e:
            log.error("movienight monitor loop error: %s", e, exc_info=True)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
