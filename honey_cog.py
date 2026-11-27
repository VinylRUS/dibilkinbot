"""v3.15 «Мёд» 🍯 — Discord-рантайм: фоновые задачи и события.

HoneyCog (подключается в main.py — без правок bot.py):
- «Поймай пчелу» #16: цикл постит 🐝 в настроенный канал, раунд живёт
  30 минут, первые 3 отреагировавших получают по 5 мёда (атомарный клейм
  в honey_economy.he_bee_claim, приз идемпотентен через dedupe_key).
- Лотерея «Улей» #14: еженедельный розыгрыш (воскресенье 20:00 UTC) —
  розыгрыш + анонс победителя с прозрачным RNG-сидом.
- Дивиденды #18: 1-го числа месяца — % от банка активным участникам.
- Ставки #15: расчёт открытых ставок по новому победителю колеса
  (polling таблицы winners + маркер honey_bets_last_winner_id — без
  врезки в wheel_routes; выигрыши ≤ 5 минут после крутки).
- Steam-бонус #8: идемпотентный бэкфилл юзерам с привязанным Steam
  (dedupe_key гарантирует разовость; работает и для старых привязок).

Настройки per-guild (guild_{id}_settings):
- honey_bee_channel_id — канал «Поймай пчелу» (фича выключена если не задан)
- honey_bee_interval_min — интервал между раундами пчелы (мин, дефолт 240)
- honey_announce_channel_id — анонсы лотереи/дивидендов/ставок (fallback: канал пчелы)
- honey_dividend_percent — % дивидендов (дефолт 5)
- honey_bets_last_winner_id — служебный маркер расчёта ставок (тех.)
"""
from __future__ import annotations

import logging
import random
from datetime import datetime

import aiosqlite
import discord
from discord.ext import commands, tasks

import db
import guild as _guild
import honey_economy as he
from config import settings

log = logging.getLogger("honey")


async def _approved_guilds() -> list[int]:
    """ID апрувнутых гильдий (реестр guilds)."""
    async with aiosqlite.connect(settings.database_path) as conn:
        async with conn.execute(
            "SELECT guild_id FROM guilds WHERE approved = 1") as cur:
            rows = await cur.fetchall()
    return [int(r[0]) for r in rows]


def _fmt_discord_ids(ids: list[int]) -> str:
    return ", ".join(f"<{i}>" for i in ids)


class HoneyCog(commands.Cog):
    """Экономика мёда: пчела, лотерея, дивиденды, ставки, Steam-бонус."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._dividends_last_month: dict[int, str] = {}  # guild_id -> YYYY-MM
        self.bee_loop.start()
        self.maintenance_loop.start()
        log.info("HoneyCog loaded (bee + maintenance loops)")

    def cog_unload(self) -> None:
        self.bee_loop.cancel()
        self.maintenance_loop.cancel()

    # ---------- Каналы ----------

    async def _channel(self, guild_id: int, key: str,
                       fallback_bee: bool = False) -> discord.abc.Messageable | None:
        ch_id = await db.g_get_setting(guild_id, key)
        if not ch_id or not str(ch_id).isdigit():
            if fallback_bee:
                ch_id = await db.g_get_setting(guild_id, "honey_bee_channel_id")
                if not ch_id or not str(ch_id).isdigit():
                    return None
            else:
                return None
        return self.bot.get_channel(int(ch_id))

    # ---------- «Поймай пчелу» (#16): постинг раундов ----------

    @tasks.loop(minutes=15)
    async def bee_loop(self) -> None:
        """Каждые 15 минут проверяет интервалы по гильдиям.

        Раунд запускается не чаще honey_bee_interval_min (дефолт 240) —
        кулдаун между раундами из скоупа. Плюс лёгкий рандом-джиттер,
        чтобы пчела не прилетала по расписанию до минуты.
        """
        try:
            for guild_id in await _approved_guilds():
                try:
                    channel = await self._channel(guild_id, "honey_bee_channel_id")
                    if channel is None:
                        continue
                    interval = await db.g_get_setting(guild_id, "honey_bee_interval_min")
                    interval_min = int(interval) if interval and interval.isdigit() else 240
                    # кулдаун: недавний открытый раунд — не постим новый
                    if await he.he_bee_has_open_recent(guild_id, interval_min):
                        continue
                    if random.random() < 0.35:  # джиттер: пропускаем тик
                        continue
                    await self._post_bee_round(guild_id, channel)
                except Exception as e:
                    log.warning("bee_loop guild %s failed: %s", guild_id, e, exc_info=True)
        except Exception as e:
            log.error("bee_loop failed: %s", e, exc_info=True)

    @bee_loop.before_loop
    async def _wait_bee(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_bee_round(self, guild_id: int,
                              channel: discord.abc.Messageable) -> None:
        try:
            message = await channel.send(
                "🐝 **Поймай пчелу!**\n"
                "Первые **3** поставившие реакцию 🐝 под этим сообщением "
                "получают по **5 мёда** 🍯\n"
                f"_(раунд живёт {he.HONEY_BEE_WINDOW_MIN} минут)_")
        except Exception as e:
            log.warning("bee round post failed guild %s: %s", guild_id, e)
            return
        await he.he_bee_round_create(guild_id, channel.id, message.id)
        log.info("bee round posted: guild=%s channel=%s message=%s",
                 guild_id, channel.id, message.id)

    # ---------- Реакция: клейм пчелы ----------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Первые 3 поставивших 🐝 на сообщение раунда получают по 5 мёда."""
        try:
            if str(payload.emoji) != "🐝":
                return
            if payload.user_id == self.bot.user.id:
                return
            if payload.guild_id is None:
                return
            guild_id = int(payload.guild_id)
            rnd = await he.he_bee_open_by_message(
                guild_id, payload.channel_id, payload.message_id)
            if not rnd:
                return
            position = await he.he_bee_claim(guild_id, rnd["id"], payload.user_id)
            if position is None:
                return  # вне тройки или уже клеймил — тихо игнорируем
            granted = await he.he_bee_prize(guild_id, rnd["id"], payload.user_id)
            if granted:
                log.info("bee prize: guild=%s user=%s round=%s pos=%s",
                         guild_id, payload.user_id, rnd["id"], position)
            if position >= he.HONEY_BEE_WINNERS:
                # тройка укомплектована — закрываем и редактируем сообщение
                claims = await he.he_bee_close_round(guild_id, rnd["id"])
                winners = [c["user_discord_id"] for c in claims]
                channel = self.bot.get_channel(payload.channel_id)
                if channel is not None:
                    try:
                        msg = await channel.fetch_message(payload.message_id)
                        await msg.edit(
                            content=msg.content +
                            f"\n\n🏆 **Пчела поймана!** Победители: "
                            f"{_fmt_discord_ids(winners)} "
                            f"(+{he.HONEY_BEE_REWARD} мёда каждому)")
                    except Exception as e:
                        log.warning("bee message edit failed: %s", e)
        except Exception as e:
            log.error("on_raw_reaction_add failed: %s", e, exc_info=True)

    # ---------- Maintenance: лотерея, дивиденды, ставки, Steam, пчёлы ----------

    @tasks.loop(minutes=5)
    async def maintenance_loop(self) -> None:
        """Раз в 5 минут по каждой гильдии:
        - закрывает протухшие раунды пчелы (окно 30 мин);
        - рассчитывает открытые ставки по новому победителю колеса;
        - начисляет Steam-бонус юзерам с привязанным профилем (идемпотентно);
        - проводит лотерею (draw_at прошло) и дивиденды (1-е число).
        """
        try:
            now = datetime.utcnow()
            for guild_id in await _approved_guilds():
                try:
                    await self._close_stale_bee_rounds(guild_id)
                    await self._maybe_settle_bets(guild_id)
                    await self._maybe_steam_bonuses(guild_id)
                    await self._maybe_draw_lottery(guild_id, now)
                    await self._maybe_pay_dividends(guild_id, now)
                except Exception as e:
                    log.warning("honey maintenance guild %s failed: %s",
                                guild_id, e, exc_info=True)
        except Exception as e:
            log.error("honey maintenance_loop failed: %s", e, exc_info=True)

    @maintenance_loop.before_loop
    async def _wait_maintenance(self) -> None:
        await self.bot.wait_until_ready()

    async def _close_stale_bee_rounds(self, guild_id: int) -> None:
        """Раунды пчелы старше окна — закрыть, отредактировать сообщение."""
        for rnd in await he.he_bee_stale_rounds(guild_id):
            claims = await he.he_bee_close_round(guild_id, rnd["id"])
            channel = self.bot.get_channel(rnd["channel_id"])
            if channel is None:
                continue
            try:
                msg = await channel.fetch_message(rnd["message_id"])
                if len(claims) >= he.HONEY_BEE_WINNERS:
                    continue  # сообщение уже отредактировано клеймом реакции
                await msg.edit(
                    content=msg.content +
                    f"\n\n⌛ Раунд завершён: успели {len(claims)}/"
                    f"{he.HONEY_BEE_WINNERS} — пчела улетела.")
            except Exception as e:
                log.warning("bee stale close failed: %s", e)

    # ---------- #15: расчёт ставок по победителю колеса ----------

    async def _last_real_winner(self, guild_id: int) -> tuple[int, str] | None:
        """Последний НЕ-тестовый победитель колеса: (id, lot_name)."""
        t_winners = _guild.guild_table(guild_id, "winners")
        async with aiosqlite.connect(settings.database_path) as conn:
            async with conn.execute(
                f"SELECT id, lot_name FROM {t_winners} WHERE is_test = 0 "
                f"ORDER BY id DESC LIMIT 1") as cur:
                row = await cur.fetchone()
        return (int(row[0]), row[1]) if row else None

    async def _maybe_settle_bets(self, guild_id: int) -> None:
        """Рассчитать открытые ставки, если появился новый победитель колеса.

        Маркер honey_bets_last_winner_id в guild settings — какой победитель
        уже обработан. Прием ставок при недавнем победителе (< 60 мин)
        блокируется в /api/honey/bets/place — ставки на известного
        победителя невозможны, гонок нет.
        """
        open_bets = await he.he_bets_open(guild_id)
        last = await self._last_real_winner(guild_id)
        marker_str = await db.g_get_setting(guild_id, "honey_bets_last_winner_id")
        marker = int(marker_str) if marker_str and marker_str.isdigit() else None

        if last is None:
            return
        if not open_bets["bets"]:
            if marker != last[0]:  # догоняем маркер без расчёта
                await db.g_set_setting(guild_id, "honey_bets_last_winner_id",
                                       str(last[0]))
            return
        if marker == last[0]:
            return  # победителя с момента ставок ещё не было

        result = await he.he_bets_settle(guild_id, last[1])
        await db.g_set_setting(guild_id, "honey_bets_last_winner_id", str(last[0]))
        if result.get("bets"):
            log.info("bets settled: guild=%s winner=%r bets=%d pool=%d "
                     "commission=%d payouts=%d",
                     guild_id, last[1], result["bets"], result["pool"],
                     result["commission"], len(result["payouts"]))
            winners = [p["user_discord_id"] for p in result["payouts"]]
            await self._announce(
                guild_id,
                f"🎡 **Расчёт ставок!** Победил «{last[1]}» — пул "
                f"**{result['pool']} мёда**. Комиссия 10% "
                f"({result['commission']}) → в банк сервера. "
                f"Выигрыши: {_fmt_discord_ids(winners) if winners else 'никто'}")

    # ---------- #8: Steam-бонус (идемпотентный бэкфилл) ----------

    async def _maybe_steam_bonuses(self, guild_id: int) -> None:
        """Разовый бонус 30 мёда всем юзерам с привязанным Steam-профилем.

        Хук не в /api/profile/steam (web.py не трогаем): идемпотентность
        гарантирует dedupe_key `steam_bonus:{uid}` — работающие привязки
        (в т.ч. сделанные до v3.15) получают бонус при первом проходе,
        задвоение исключено.
        """
        async with aiosqlite.connect(settings.database_path) as conn:
            async with conn.execute(
                "SELECT discord_id FROM users "
                "WHERE steam_id64 IS NOT NULL AND steam_id64 != ''") as cur:
                rows = await cur.fetchall()
        for (uid,) in rows:
            if await he.he_steam_bonus(guild_id, int(uid)):
                log.info("steam bonus granted: guild=%s user=%s", guild_id, uid)

    # ---------- #14: лотерея ----------

    async def _announce(self, guild_id: int, content: str) -> None:
        channel = await self._channel(guild_id, "honey_announce_channel_id",
                                      fallback_bee=True)
        if channel is None:
            return
        try:
            await channel.send(content)
        except Exception as e:
            log.warning("honey announce failed guild %s: %s", guild_id, e)

    async def _maybe_draw_lottery(self, guild_id: int, now: datetime) -> None:
        result = await he.he_lottery_draw_due(guild_id, now)
        if not result:
            return
        if result.get("rolled"):
            await self._announce(
                guild_id,
                "🎟️ **Лотерея «Улей»** — участников меньше минимума "
                f"({he.LOTTERY_MIN_PARTICIPANTS}), билеты возвращены. "
                "Розыгрыш переносится на следующую неделю.")
            return
        winner, prize = result["winner"], result["prize"]
        await self._announce(
            guild_id,
            f"🎟️ **Лотерея «Улей» — розыгрыш!**\n"
            f"Победитель: <@{winner}> — **+{prize} мёда** 🍯\n"
            f"Билетов в розыгрыше: {result['tickets']}\n"
            f"Прозрачный RNG: `seed={result['seed']}` → roll "
            f"`{result['roll']} % {result['tickets']}` — победитель "
            "пересчитывается по этим данным.")

    # ---------- #18: дивиденды ----------

    async def _maybe_pay_dividends(self, guild_id: int, now: datetime) -> None:
        month_key = f"{now:%Y-%m}"
        if now.day != 1:
            return
        if self._dividends_last_month.get(guild_id) == month_key:
            return
        percent = await db.g_get_setting(guild_id, "honey_dividend_percent")
        percent = int(percent) if percent and percent.isdigit() \
            else he.DIVIDEND_PERCENT_DEFAULT
        result = await he.he_dividends_run(guild_id, percent=percent, now=now)
        self._dividends_last_month[guild_id] = month_key
        if result.get("paid"):
            await self._announce(
                guild_id,
                f"🍯 **Дивиденды от банка!**\n"
                f"Активным участникам ({result['users']}) выплачено по "
                f"**{result['per_user']} мёда** — {result['percent']}% банка сервера.")
        else:
            # банк пуст/мал — тихо (анонс не нужен)
            log.info("dividends skipped guild %s: %s", guild_id, result)
