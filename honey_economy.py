"""v3.15 «Мёд» 🍯 — экономика: общий банк сервера, подарки, ставки,
лотерея «Улей», «Поймай пчелу», дивиденды, гильдии, уровни, Steam-бонус.

Скоуп v3.15.0 (решения босса от 2026-11-27):
- Комиссии (подарки #31, ставки #15) идут НЕ в сгорание, а в общий банк сервера.
- Расходовать банк может только Матка (superuser) — вручную или дивидендами #18.
- Лиги #51 упразднены → гильдии создаёт/управляет только Матка.
- GIF-рамки #45 — финальный этап версии (таблица/механика готова, графика — открытый вопрос).

Технический фундамент переиспользуется из v3.11:
- honey_ledger (per-guild) + g_honey_spend / g_honey_balance из db.py;
- g_cosmetic_buy(guild, user, type, value, cost) — косметика;
- фоновые задачи — паттерн _spawn_background_task в bot.py.

Открытые вопросы скоупа решены дефолтами (см. CHANGELOG v3.15.0 «Дефолты
открытых вопросов»): лотерея — приз 500 эмиссией, выручка от билетов → в банк;
дивиденды — 5% банка 1-го числа, «активный» = активность за 30 дней;
Steam-бонус — за привязку профиля (вишлист опционален).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta

import aiosqlite

import guild as _guild
import db as _db  # g_honey_spend / g_honey_balance / g_cosmetic_buy — без circular: db.py нас не импортирует
from config import settings

log = logging.getLogger("honey")

# ============================================================
# Константы v3.15
# ============================================================
HONEY_GIFT_COMMISSION = 0.10   # #31: комиссия P2P-перевода → банк
BETS_COMMISSION = 0.10        # #15: комиссия пула ставок → банк
HONEY_STEAM_BONUS = 30         # #8: разовый бонус за привязку Steam
HONEY_BEE_REWARD = 5           # #16: награда за пойманную пчелу
HONEY_BEE_WINNERS = 3          # #16: первые N успевших получают приз
HONEY_BEE_WINDOW_MIN = 30       # #16: сколько минут живёт раунд пчелы
LOTTERY_TICKET_COST = 10       # #14: цена билета
LOTTERY_PRIZE = 500            # #14: приз (фиксированный)
LOTTERY_MIN_PARTICIPANTS = 3   # #14: анти-абьюз — минимум уникальных участников
LOTTERY_DRAW_HOUR = 20         # #14: час розыгрыша (UTC), воскресенье
DIVIDEND_PERCENT_DEFAULT = 5  # #18: % от банка раз в месяц
DIVIDEND_ACTIVE_DAYS = 30      # #18: окно «активности» участника
HONEY_LEVEL_STEP = 100         # #52: мёда на уровень
FRAME_COST = 100              # #27: статичная рамка
GIF_FRAME_COST = 200           # #45: GIF-рамка (финальный этап)

# Каталог рамок. Графика — открытый вопрос скоупа (кто рисует?),
# пока рендерится текстом/эмодзи в UI.
FRAMES: dict[str, str] = {
    "pasechnik": "Почётный Пасечник",
    "keeper": "Хранитель Улья",
    "night_bee": "Ночная Пчела",
    "medovik": "Медовик",
}
GIF_FRAMES: dict[str, str] = {
    "bee_mechanic": "Пчела-механик 🐝⚙️",
}

# dedupe_key миграция для honey_ledger — ensure один раз на процесс/гильдию
_dedupe_ready: set[int] = set()


def _connect():
    return aiosqlite.connect(settings.database_path)


def _now() -> str:
    return datetime.utcnow().isoformat()


# ============================================================
# dedupe_key: фундамент идемпотентности разовых бонусов
# ============================================================

async def ensure_dedupe(guild_id: int) -> None:
    """Добавить колонку dedupe_key + UNIQUE-индекс в honey_ledger если их нет.

    На свежих гильдиях это делает guild.init_guild_tables (v3.15 миграция);
    здесь — ленивая страховка на случай web-only старта (бот не поднят, on_ready
    не выполнился). Идемпотентна.
    """
    gid = int(guild_id)
    if gid in _dedupe_ready:
        return
    table = _guild.guild_table(gid, "honey_ledger")
    async with _connect() as conn:
        async with conn.execute(f"PRAGMA table_info({table})") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "dedupe_key" not in cols:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN dedupe_key TEXT")
            log.info("v3.15 lazy migration: dedupe_key added to %s", table)
        await conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_dedupe ON {table}(dedupe_key)")
        await conn.commit()
    _dedupe_ready.add(gid)


async def _ledger_insert(guild_id: int, user_discord_id: int, delta: int,
                         reason: str | None, dedupe_key: str | None = None,
                         created_by: int | None = None) -> bool:
    """Вставить строку в honey_ledger. False если dedupe_key уже существует.

    INSERT с уникальным dedupe_key атомарно защищает разовые бонусы (#8/#16/#18)
    от задвоения при рестарте фоновых задач.
    """
    gid = int(guild_id)
    table = _guild.guild_table(gid, "honey_ledger")
    for attempt in range(2):
        try:
            async with _connect() as conn:
                await conn.execute(
                    f"INSERT INTO {table} (user_discord_id, delta, reason, created_by, created_at, dedupe_key) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    (int(user_discord_id), int(delta), reason,
                     int(created_by) if created_by is not None else None,
                     _now(), dedupe_key),
                )
                await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # уже было начислено с этим dedupe_key
        except aiosqlite.OperationalError as e:
            if attempt == 0 and "dedupe_key" in str(e):
                await ensure_dedupe(gid)
                continue
            raise
    return False


async def he_grant_dedupe(guild_id: int, user_discord_id: int, amount: int,
                          reason: str, dedupe_key: str) -> bool:
    """Начислить разовый бонус идемпотентно. True = начислили сейчас, False = уже был."""
    if int(amount) == 0:
        return False
    return await _ledger_insert(guild_id, user_discord_id, int(amount),
                                reason, dedupe_key=dedupe_key)


# ============================================================
# #8: Steam-бонус (30 мёда за привязку профиля)
# ============================================================

async def he_steam_bonus(guild_id: int, user_discord_id: int) -> bool:
    """Разовый Steam-бонус. Вишлист опционален (открытый вопрос #7 скоупа)."""
    return await he_grant_dedupe(guild_id, user_discord_id, HONEY_STEAM_BONUS,
                                 reason="steam_link_bonus",
                                 dedupe_key=f"steam_bonus:{user_discord_id}")


# ============================================================
# Общий банк сервера (ядро экономики v3.15)
# ============================================================

async def he_bank_balance(guild_id: int) -> int:
    table = _guild.guild_table(guild_id, "honey_bank")
    async with _connect() as conn:
        async with conn.execute(f"SELECT balance FROM {table} WHERE id = 1") as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def _bank_apply(guild_id: int, delta: int, reason: str | None,
                      actor_discord_id: int | None) -> int | None:
    """Атомарно применить delta к банку (одна транзакция: UPDATE + лог).

    Возвращает баланс после операции или None (недостаточно средств при списании).
    """
    gid = int(guild_id)
    t_bank = _guild.guild_table(gid, "honey_bank")
    t_log = _guild.guild_table(gid, "honey_bank_log")
    async with _connect() as conn:
        await conn.execute(
            f"INSERT OR IGNORE INTO {t_bank} (id, balance, updated_at) VALUES (1, 0, ?)",
            (_now(),))
        if delta < 0:
            cur = await conn.execute(
                f"UPDATE {t_bank} SET balance = balance + ?, updated_at = ? "
                f"WHERE id = 1 AND balance >= ?",
                (delta, _now(), -delta))
            if cur.rowcount == 0:
                await conn.commit()
                return None  # недостаточно средств в банке
        else:
            await conn.execute(
                f"UPDATE {t_bank} SET balance = balance + ?, updated_at = ? WHERE id = 1",
                (delta, _now()))
        async with conn.execute(f"SELECT balance FROM {t_bank} WHERE id = 1") as cur:
            row = await cur.fetchone()
        balance_after = int(row[0])
        await conn.execute(
            f"INSERT INTO {t_log} (delta, balance_after, reason, actor_discord_id, created_at) "
            f"VALUES (?, ?, ?, ?, ?)",
            (int(delta), balance_after, reason,
             int(actor_discord_id) if actor_discord_id is not None else None,
             _now()))
        await conn.commit()
    return balance_after


async def he_bank_deposit(guild_id: int, amount: int, reason: str | None,
                          actor_discord_id: int | None = None) -> bool:
    """Пополнить банк (комиссии, ручные начисления Матки). amount > 0."""
    if int(amount) <= 0:
        return False
    return await _bank_apply(guild_id, int(amount), reason, actor_discord_id) is not None


async def he_bank_withdraw(guild_id: int, amount: int, reason: str | None,
                           actor_discord_id: int | None = None) -> bool:
    """Списать из банка (только Матка: призы, дивиденды). Банк не уходит в минус."""
    if int(amount) <= 0:
        return False
    return await _bank_apply(guild_id, -int(amount), reason, actor_discord_id) is not None


async def he_bank_log(guild_id: int, limit: int = 50) -> list[dict]:
    table = _guild.guild_table(guild_id, "honey_bank_log")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, delta, balance_after, reason, actor_discord_id, created_at "
            f"FROM {table} ORDER BY id DESC LIMIT ?", (int(limit),)) as cur:
            rows = await cur.fetchall()
    return [{"id": r[0], "delta": int(r[1]), "balance_after": int(r[2]),
             "reason": r[3], "actor_discord_id": r[4], "created_at": r[5]}
            for r in rows]


# ============================================================
# #31: Подарочный мёд (P2P-перевод с комиссией 10% → банк)
# ============================================================

async def he_gift(guild_id: int, from_uid: int, to_uid: int, amount: int) -> dict:
    """P2P-перевод мёда. Получатель получает `amount`, отправитель платит
    amount + 10% комиссии, комиссия → общий банк сервера.

    Комиссия сама душит перекидку ради фарма — доп. лимиты не вводим (скоуп).
    Возвращает {ok, error?, commission, delivered, balance}.
    """
    amount = int(amount)
    from_uid, to_uid = int(from_uid), int(to_uid)
    if amount <= 0:
        return {"ok": False, "error": "amount"}
    if from_uid == to_uid:
        return {"ok": False, "error": "self"}

    # получатель должен существовать (панельный юзер)
    async with _connect() as conn:
        async with conn.execute(
            "SELECT display_name, username FROM users WHERE discord_id = ?", (to_uid,)) as cur:
            row = await cur.fetchone()
    if not row:
        return {"ok": False, "error": "recipient_not_found"}

    commission = max(1, int(round(amount * HONEY_GIFT_COMMISSION)))
    total_charge = amount + commission
    if not await _db.g_honey_spend(guild_id, from_uid, total_charge,
                                   reason=f"gift_to:{to_uid}:{amount}"):
        return {"ok": False, "error": "insufficient",
                "balance": await _db.g_honey_balance(guild_id, from_uid)}

    await _ledger_insert(guild_id, to_uid, amount, reason=f"gift_from:{from_uid}")
    await he_bank_deposit(guild_id, commission, reason=f"gift_commission:{from_uid}->{to_uid}")

    return {"ok": True, "commission": commission, "delivered": amount,
            "balance": await _db.g_honey_balance(guild_id, from_uid)}


# ============================================================
# #52: Прогресс-бар уровня (каждые 100 мёда = уровень)
# ============================================================

async def he_level(guild_id: int, user_discord_id: int) -> dict:
    """Уровень от СУММАРНО заработанного (не падает при тратах).

    total_earned = SQL-заработок (watched/ratings/quotes/achievements)
    + положительные delta леджера (бонусы, выигрыши, подарки).
    Возвращает {total_earned, level, progress, next_at}.
    """
    uid = int(user_discord_id)
    bd = await _db.g_honey_breakdown(guild_id, uid)
    t_ledger = _guild.guild_table(guild_id, "honey_ledger")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT COALESCE(SUM(delta), 0) FROM {t_ledger} "
            f"WHERE user_discord_id = ? AND delta > 0", (uid,)) as cur:
            positives = int((await cur.fetchone())[0])
    total_earned = int(bd["earned"]) + positives
    level = total_earned // HONEY_LEVEL_STEP
    return {
        "total_earned": total_earned,
        "level": level,
        "progress": total_earned % HONEY_LEVEL_STEP,
        "step": HONEY_LEVEL_STEP,
        "next_at": HONEY_LEVEL_STEP - (total_earned % HONEY_LEVEL_STEP),
    }


# ============================================================
# #14: Лотерея «Улей» (еженедельный розыгрыш)
# ============================================================

def _week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _next_draw_at(now: datetime) -> datetime:
    """Ближайшее воскресенье 20:00 UTC (если уже прошло — следующее)."""
    days_ahead = (6 - now.weekday()) % 7  # воскресенье = 6
    draw = now.replace(hour=LOTTERY_DRAW_HOUR, minute=0, second=0, microsecond=0)
    draw = draw + timedelta(days=days_ahead)
    if draw <= now:
        draw += timedelta(days=7)
    return draw


async def he_lottery_ensure(guild_id: int, now: datetime | None = None) -> dict:
    """Текущий открытый раунд недели; создаётся лениво при первом обращении.

    Выручка от билетов → общий банк (фонд дивидендов #18); приз — 500 эмиссией
    (дефолт открытого вопроса #3 скоупа).
    """
    now = now or datetime.utcnow()
    table = _guild.guild_table(guild_id, "honey_lottery_rounds")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, week_key, status, ticket_cost, prize, min_participants, draw_at, "
            f"winner_discord_id, rng_seed, rng_roll, drawn_at, created_at "
            f"FROM {table} WHERE week_key = ? AND status = 'open'",
            (_week_key(now),)) as cur:
            row = await cur.fetchone()
        if row:
            return _round_row(row)
        await conn.execute(
            f"INSERT INTO {table} (week_key, status, ticket_cost, prize, min_participants, "
            f"draw_at, created_at) VALUES (?, 'open', ?, ?, ?, ?, ?)",
            (_week_key(now), LOTTERY_TICKET_COST, LOTTERY_PRIZE,
             LOTTERY_MIN_PARTICIPANTS, _next_draw_at(now).isoformat(), _now()))
        await conn.commit()
        async with conn.execute(
            f"SELECT id, week_key, status, ticket_cost, prize, min_participants, draw_at, "
            f"winner_discord_id, rng_seed, rng_roll, drawn_at, created_at "
            f"FROM {table} WHERE week_key = ? AND status = 'open'",
            (_week_key(now),)) as cur:
            row = await cur.fetchone()
    return _round_row(row)


def _round_row(r) -> dict:
    return {"id": r[0], "week_key": r[1], "status": r[2], "ticket_cost": r[3],
            "prize": r[4], "min_participants": r[5], "draw_at": r[6],
            "winner_discord_id": r[7], "rng_seed": r[8], "rng_roll": r[9],
            "drawn_at": r[10], "created_at": r[11]}


async def he_lottery_buy(guild_id: int, user_discord_id: int, qty: int = 1) -> dict:
    """Купить билеты текущего раунда. Мёд списывается сразу, выручка → банк."""
    qty = int(qty)
    uid = int(user_discord_id)
    if qty < 1 or qty > 100:
        return {"ok": False, "error": "qty"}
    rnd = await he_lottery_ensure(guild_id)
    cost = qty * int(rnd["ticket_cost"])
    if not await _db.g_honey_spend(guild_id, uid, cost,
                                   reason=f"lottery_ticket:{rnd['id']}:{qty}"):
        return {"ok": False, "error": "insufficient",
                "balance": await _db.g_honey_balance(guild_id, uid)}
    t_tickets = _guild.guild_table(guild_id, "honey_lottery_tickets")
    async with _connect() as conn:
        await conn.execute(
            f"INSERT INTO {t_tickets} (round_id, user_discord_id, qty, cost, created_at) "
            f"VALUES (?, ?, ?, ?, ?)",
            (rnd["id"], uid, qty, cost, _now()))
        await conn.commit()
    await he_bank_deposit(guild_id, cost, reason=f"lottery_tickets:{rnd['id']}")
    return {"ok": True, "qty": qty, "cost": cost,
            "balance": await _db.g_honey_balance(guild_id, uid)}


async def he_lottery_info(guild_id: int, user_discord_id: int | None = None) -> dict:
    """Состояние лотереи: раунд + статистика + мои билеты."""
    rnd = await he_lottery_ensure(guild_id)
    t_tickets = _guild.guild_table(guild_id, "honey_lottery_tickets")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT COALESCE(SUM(qty), 0), COUNT(*), COUNT(DISTINCT user_discord_id) "
            f"FROM {t_tickets} WHERE round_id = ?", (rnd["id"],)) as cur:
            r = await cur.fetchone()
        my_qty = 0
        if user_discord_id is not None:
            async with conn.execute(
                f"SELECT COALESCE(SUM(qty), 0) FROM {t_tickets} "
                f"WHERE round_id = ? AND user_discord_id = ?",
                (rnd["id"], int(user_discord_id))) as cur:
                my_qty = int((await cur.fetchone())[0])
    return {"round": rnd, "tickets_total": int(r[0]), "purchases": int(r[1]),
            "participants": int(r[2]), "my_tickets": my_qty,
            "pool": int(r[0]) * int(rnd["ticket_cost"])}


async def he_lottery_draw_due(guild_id: int, now: datetime | None = None) -> dict | None:
    """Провести розыгрыш, если draw_at текущего раунда прошло. None = не пора.

    Прозрачный RNG: seed = sha256(round_id:week_key:drawn_at), roll = seed % total.
    Seed и roll публикуются (в строке раунда + в анонсе) — каждый может
    пересчитать победителя.
    """
    now = now or datetime.utcnow()
    table = _guild.guild_table(guild_id, "honey_lottery_rounds")
    t_tickets = _guild.guild_table(guild_id, "honey_lottery_tickets")

    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id FROM {table} WHERE week_key = ? AND status = 'open' AND draw_at <= ?",
            (_week_key(now), now.isoformat())) as cur:
            r = await cur.fetchone()
    if not r:
        return None
    round_id = int(r[0])

    drawn_at = _now()
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT user_discord_id, qty FROM {t_tickets} WHERE round_id = ?",
            (round_id,)) as cur:
            tickets = await cur.fetchall()

    # Анти-абьюз: мало участников → возврат билетов, раунд «перенесён»
    participants = {int(t[0]) for t in tickets}
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT prize, min_participants FROM {table} WHERE id = ?",
            (round_id,)) as cur:
            cfg = await cur.fetchone()
    prize, min_participants = int(cfg[0]), int(cfg[1])

    if len(participants) < min_participants:
        for uid in participants:
            refund = sum(int(t[1]) for t in tickets if int(t[0]) == uid) * LOTTERY_TICKET_COST
            await he_grant_dedupe(guild_id, uid, refund,
                                  reason=f"lottery_refund:{round_id}",
                                  dedupe_key=f"lottery_refund:{round_id}:{uid}")
        async with _connect() as conn:
            await conn.execute(
                f"UPDATE {table} SET status = 'rolled', drawn_at = ? WHERE id = ? "
                f"AND status = 'open'",
                (drawn_at, round_id))
            await conn.commit()
        return {"rolled": True, "round_id": round_id,
                "participants": len(participants), "refunded": True}

    # Прозрачный розыгрыш: раскрываем tickets в «стрип» и крутим roll по seed
    strip: list[int] = []
    for uid, qty in tickets:
        strip.extend([int(uid)] * int(qty))
    seed_str = f"{round_id}:{_week_key(now)}:{drawn_at}"
    seed = hashlib.sha256(seed_str.encode()).hexdigest()
    roll = int(seed, 16) % len(strip)
    winner = strip[roll]

    await he_grant_dedupe(guild_id, winner, prize, reason=f"lottery_win:{round_id}",
                          dedupe_key=f"lottery_win:{round_id}:{winner}")
    async with _connect() as conn:
        await conn.execute(
            f"UPDATE {table} SET status = 'drawn', winner_discord_id = ?, "
            f"rng_seed = ?, rng_roll = ?, drawn_at = ? WHERE id = ? AND status = 'open'",
            (winner, seed, roll, drawn_at, round_id))
        await conn.commit()
    return {"rolled": False, "round_id": round_id, "winner": winner, "prize": prize,
            "seed": seed, "roll": roll, "tickets": len(strip)}


async def he_lottery_last(guild_id: int) -> dict | None:
    """Последний завершённый раунд (для анонсов/страницы)."""
    table = _guild.guild_table(guild_id, "honey_lottery_rounds")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, week_key, status, ticket_cost, prize, min_participants, draw_at, "
            f"winner_discord_id, rng_seed, rng_roll, drawn_at, created_at "
            f"FROM {table} WHERE status IN ('drawn', 'rolled') ORDER BY id DESC LIMIT 1") as cur:
            row = await cur.fetchone()
    return _round_row(row) if row else None


# ============================================================
# #15: Ставки мёдом на сектор колеса
# ============================================================

async def he_bet_place(guild_id: int, user_discord_id: int, item_name: str,
                       amount: int) -> dict:
    """Поставить мёд на сектор. Списывается сразу, статус 'open'.

    Окно приёма: пока колесо не крутится (роутер проверяет lock);
    расчёт — в wheel_routes._delayed_complete_spin при определении победителя.
    """
    amount = int(amount)
    uid = int(user_discord_id)
    if amount < 1 or amount > 10_000:
        return {"ok": False, "error": "amount"}
    if not await _db.g_honey_spend(guild_id, uid, amount,
                                   reason=f"bet:{item_name}:{amount}"):
        return {"ok": False, "error": "insufficient",
                "balance": await _db.g_honey_balance(guild_id, uid)}
    table = _guild.guild_table(guild_id, "honey_bets")
    async with _connect() as conn:
        await conn.execute(
            f"INSERT INTO {table} (user_discord_id, item_name, amount, status, created_at) "
            f"VALUES (?, ?, ?, 'open', ?)",
            (uid, item_name, amount, _now()))
        await conn.commit()
    return {"ok": True, "amount": amount, "item_name": item_name,
            "balance": await _db.g_honey_balance(guild_id, uid)}


async def he_bets_open(guild_id: int) -> dict:
    """Открытые ставки + пул. Возвращает {bets, pool, by_item}."""
    table = _guild.guild_table(guild_id, "honey_bets")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, user_discord_id, item_name, amount, created_at FROM {table} "
            f"WHERE status = 'open' ORDER BY id", ()) as cur:
            rows = await cur.fetchall()
    bets = [{"id": r[0], "user_discord_id": r[1], "item_name": r[2],
             "amount": int(r[3]), "created_at": r[4]} for r in rows]
    by_item: dict[str, int] = {}
    for b in bets:
        by_item[b["item_name"]] = by_item.get(b["item_name"], 0) + b["amount"]
    return {"bets": bets, "pool": sum(b["amount"] for b in bets), "by_item": by_item}


async def he_bets_settle(guild_id: int, winner_name: str) -> dict:
    """Расчёт ставок при победителе колеса.

    Победители (ставка на имя победителя) делят 90% пула пропорционально ставке,
    10% → общий банк сервера (решение босса). Проигравшие — статус 'lost'.
    Идемпотентно: расчёт по всем ставкам status='open' в одной транзакции.
    """
    gid = int(guild_id)
    table = _guild.guild_table(gid, "honey_bets")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, user_discord_id, item_name, amount FROM {table} "
            f"WHERE status = 'open'", ()) as cur:
            rows = await cur.fetchall()
    if not rows:
        return {"bets": 0, "pool": 0, "commission": 0, "payouts": []}

    pool = sum(int(r[3]) for r in rows)
    winners = [r for r in rows if r[2] == winner_name]
    commission = max(0, int(round(pool * BETS_COMMISSION)))
    payout_pool = pool - commission
    winner_stake = sum(int(r[3]) for r in winners)

    payouts: list[dict] = []
    now = _now()
    async with _connect() as conn:
        for r in rows:
            if r[2] == winner_name:
                share = (payout_pool * int(r[3]) // winner_stake) if winner_stake else 0
                payouts.append({"user_discord_id": int(r[1]), "payout": int(share)})
                await conn.execute(
                    f"UPDATE {table} SET status = 'won', payout = ?, settled_at = ? "
                    f"WHERE id = ? AND status = 'open'",
                    (int(share), now, r[0]))
            else:
                await conn.execute(
                    f"UPDATE {table} SET status = 'lost', payout = 0, settled_at = ? "
                    f"WHERE id = ? AND status = 'open'",
                    (now, r[0]))
        await conn.commit()

    for p in payouts:
        await _ledger_insert(guild_id, p["user_discord_id"], p["payout"],
                             reason=f"bet_win:{winner_name}")
    if commission > 0:
        await he_bank_deposit(guild_id, commission, reason=f"bets_commission:{winner_name}")

    return {"bets": len(rows), "pool": pool, "commission": commission,
            "winner_stake": winner_stake, "payouts": payouts}


async def he_bets_history(guild_id: int, user_discord_id: int | None = None,
                          limit: int = 20) -> list[dict]:
    """История расчёта ставок (последние закрытые)."""
    table = _guild.guild_table(guild_id, "honey_bets")
    sql = (f"SELECT id, user_discord_id, item_name, amount, status, payout, settled_at "
           f"FROM {table} WHERE status != 'open'")
    params: tuple = ()
    if user_discord_id is not None:
        sql += " AND user_discord_id = ?"
        params = (int(user_discord_id),)
    sql += " ORDER BY settled_at DESC, id DESC LIMIT ?"
    params = params + (int(limit),)
    async with _connect() as conn:
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [{"id": r[0], "user_discord_id": r[1], "item_name": r[2],
             "amount": int(r[3]), "status": r[4], "payout": r[5],
             "settled_at": r[6]} for r in rows]


# ============================================================
# #16: «Поймай пчелу» (первые 3 успевших получают по 5 мёда)
# ============================================================

async def he_bee_round_create(guild_id: int, channel_id: int, message_id: int) -> int:
    table = _guild.guild_table(guild_id, "honey_bee_rounds")
    async with _connect() as conn:
        await conn.execute(
            f"INSERT INTO {table} (channel_id, message_id, status, created_at) "
            f"VALUES (?, ?, 'open', ?)",
            (int(channel_id), int(message_id), _now()))
        await conn.commit()
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            return int((await cur.fetchone())[0])


async def he_bee_open_by_message(guild_id: int, channel_id: int,
                                 message_id: int) -> dict | None:
    table = _guild.guild_table(guild_id, "honey_bee_rounds")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, status, created_at FROM {table} "
            f"WHERE channel_id = ? AND message_id = ? AND status = 'open'",
            (int(channel_id), int(message_id))) as cur:
            r = await cur.fetchone()
    if not r:
        return None
    return {"id": int(r[0]), "status": r[1], "created_at": r[2]}


async def he_bee_claim(guild_id: int, round_id: int, user_discord_id: int) -> int | None:
    """Атомарный клейм: INSERT-SELECT со счётчиком занятых слотов.

    UNIQUE(round_id, user) + ограничение COUNT<3 прямо в INSERT-выражении —
    гонки невозможны даже при одновременных реакциях.
    Возвращает позицию 1..3 или None (вне тройки / уже клеймил / раунд закрыт).
    """
    table = _guild.guild_table(guild_id, "honey_bee_claims")
    async with _connect() as conn:
        cur = await conn.execute(
            f"INSERT INTO {table} (round_id, user_discord_id, position, created_at) "
            f"SELECT ?, ?, "
            f"(SELECT COALESCE(MAX(position), 0) + 1 FROM {table} WHERE round_id = ?), ? "
            f"WHERE (SELECT COUNT(*) FROM {table} WHERE round_id = ?) < ? "
            f"AND NOT EXISTS (SELECT 1 FROM {table} WHERE round_id = ? AND user_discord_id = ?)",
            (int(round_id), int(user_discord_id), int(round_id), _now(),
             int(round_id), HONEY_BEE_WINNERS, int(round_id), int(user_discord_id)))
        await conn.commit()
        if cur.rowcount == 0:
            return None
        async with conn.execute(
            f"SELECT position FROM {table} WHERE round_id = ? AND user_discord_id = ?",
            (int(round_id), int(user_discord_id))) as cur2:
            r = await cur2.fetchone()
    return int(r[0]) if r else None


async def he_bee_close_round(guild_id: int, round_id: int) -> list[dict]:
    """Закрыть раунд, вернуть клеймов (для анонса победителей)."""
    t_rounds = _guild.guild_table(guild_id, "honey_bee_rounds")
    t_claims = _guild.guild_table(guild_id, "honey_bee_claims")
    async with _connect() as conn:
        await conn.execute(
            f"UPDATE {t_rounds} SET status = 'closed', closed_at = ? "
            f"WHERE id = ? AND status = 'open'",
            (_now(), int(round_id)))
        await conn.commit()
        async with conn.execute(
            f"SELECT user_discord_id, position FROM {t_claims} "
            f"WHERE round_id = ? ORDER BY position", (int(round_id),)) as cur:
            rows = await cur.fetchall()
    return [{"user_discord_id": int(r[0]), "position": int(r[1])} for r in rows]


async def he_bee_stale_rounds(guild_id: int, max_minutes: int = HONEY_BEE_WINDOW_MIN) -> list[dict]:
    """Открытые раунды старше окна — кандидатуры на закрытие (кулдаун между раундами)."""
    t_rounds = _guild.guild_table(guild_id, "honey_bee_rounds")
    cutoff = (datetime.utcnow() - timedelta(minutes=max_minutes)).isoformat()
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, channel_id, message_id FROM {t_rounds} "
            f"WHERE status = 'open' AND created_at < ?", (cutoff,)) as cur:
            rows = await cur.fetchall()
    return [{"id": int(r[0]), "channel_id": int(r[1]), "message_id": int(r[2])}
            for r in rows]


async def he_bee_has_open_recent(guild_id: int, minutes: int) -> bool:
    """Есть ли открытый раунд пчелы моложе `minutes` — кулдаун постинга новых."""
    t_rounds = _guild.guild_table(guild_id, "honey_bee_rounds")
    cutoff = (datetime.utcnow() - timedelta(minutes=int(minutes))).isoformat()
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT 1 FROM {t_rounds} WHERE status = 'open' AND created_at >= ? LIMIT 1",
            (cutoff,)) as cur:
            return (await cur.fetchone()) is not None


async def he_bee_prize(guild_id: int, round_id: int, user_discord_id: int) -> bool:
    """5 мёда за пойманную пчелу — идемпотентно (dedupe по раунду)."""
    return await he_grant_dedupe(guild_id, user_discord_id, HONEY_BEE_REWARD,
                                reason="bee_catch",
                                dedupe_key=f"bee:{round_id}:{user_discord_id}")


# ============================================================
# #18: Дивиденды от банка (% раз в месяц активным участникам)
# ============================================================

async def he_dividends_run(guild_id: int, percent: int = DIVIDEND_PERCENT_DEFAULT,
                           active_days: int = DIVIDEND_ACTIVE_DAYS,
                           now: datetime | None = None) -> dict:
    """Ежемесячные дивиденды: % от банка, делится поровну между активными.

    «Активный» = есть записи в activity_log за последние `active_days` дней.
    Раз в месяц гарантирует dedupe_key dividends:{YYYY-MM}:{uid}.
    Возвращает {paid, per_user, users, fund}.
    """
    now = now or datetime.utcnow()
    month_key = f"{now:%Y-%m}"
    bank = await he_bank_balance(guild_id)
    fund = int(bank * int(percent) / 100)
    result = {"paid": False, "fund": fund, "per_user": 0, "users": 0, "percent": percent}
    if fund < 1:
        return result

    t_act = _guild.guild_table(guild_id, "activity_log")
    cutoff = (now - timedelta(days=int(active_days))).isoformat()
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT DISTINCT actor_discord_id FROM {t_act} "
            f"WHERE created_at >= ? AND actor_discord_id IS NOT NULL",
            (cutoff,)) as cur:
            rows = await cur.fetchall()
    users = [int(r[0]) for r in rows]
    if not users:
        return result

    per_user = fund // len(users)
    if per_user < 1:
        return result

    granted = 0
    for uid in users:
        if await he_grant_dedupe(guild_id, uid, per_user,
                                 reason=f"dividends:{month_key}",
                                 dedupe_key=f"dividends:{month_key}:{uid}"):
            granted += 1
    total = per_user * granted
    if granted:
        await he_bank_withdraw(guild_id, total,
                               reason=f"dividends:{month_key}", actor_discord_id=None)
    result.update({"paid": granted > 0, "per_user": per_user,
                   "users": granted, "fund": total})
    return result


# ============================================================
# Гильдии (кланы) — создаёт и управляет только Матка (#51 → новая фича)
# ============================================================

async def he_clan_create(guild_id: int, name: str, tag: str | None,
                         description: str | None, created_by: int) -> dict | None:
    """Создать гильдию. None — имя занято/невалидно."""
    name = (name or "").strip()
    if not name or len(name) > 64:
        return None
    table = _guild.guild_table(guild_id, "honey_clans")
    try:
        async with _connect() as conn:
            await conn.execute(
                f"INSERT INTO {table} (name, tag, description, created_by, created_at) "
                f"VALUES (?, ?, ?, ?, ?)",
                (name, (tag or "").strip()[:8] or None,
                 (description or "").strip()[:256] or None,
                 int(created_by), _now()))
            await conn.commit()
    except aiosqlite.IntegrityError:
        return None
    return await he_clan_by_name(guild_id, name)


async def he_clan_by_name(guild_id: int, name: str) -> dict | None:
    t_clans = _guild.guild_table(guild_id, "honey_clans")
    t_members = _guild.guild_table(guild_id, "honey_clan_members")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, name, tag, description, created_by, created_at FROM {t_clans} "
            f"WHERE name = ?", (name,)) as cur:
            r = await cur.fetchone()
        if not r:
            return None
        async with conn.execute(
            f"SELECT COUNT(*) FROM {t_members} WHERE clan_id = ?", (r[0],)) as cur:
            cnt = int((await cur.fetchone())[0])
    return _clan_row(r, cnt)


def _clan_row(r, members: int) -> dict:
    return {"id": int(r[0]), "name": r[1], "tag": r[2], "description": r[3],
            "created_by": int(r[4]), "created_at": r[5], "members": members}


async def he_clan_list(guild_id: int) -> list[dict]:
    t_clans = _guild.guild_table(guild_id, "honey_clans")
    t_members = _guild.guild_table(guild_id, "honey_clan_members")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT id, name, tag, description, created_by, created_at FROM {t_clans} "
            f"ORDER BY id") as cur:
            clans = await cur.fetchall()
        counts: dict[int, int] = {}
        async with conn.execute(
            f"SELECT clan_id, COUNT(*) FROM {t_members} GROUP BY clan_id") as cur:
            for cid, cnt in await cur.fetchall():
                counts[int(cid)] = int(cnt)
    return [_clan_row(r, counts.get(int(r[0]), 0)) for r in clans]


async def he_clan_update(guild_id: int, clan_id: int, name: str | None = None,
                         tag: str | None = None, description: str | None = None) -> bool:
    table = _guild.guild_table(guild_id, "honey_clans")
    sets, params = [], []
    if name is not None and name.strip():
        sets.append("name = ?"); params.append(name.strip()[:64])
    if tag is not None:
        sets.append("tag = ?"); params.append(tag.strip()[:8] or None)
    if description is not None:
        sets.append("description = ?"); params.append(description.strip()[:256] or None)
    if not sets:
        return False
    params.append(int(clan_id))
    try:
        async with _connect() as conn:
            await conn.execute(
                f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", tuple(params))
            await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def he_clan_delete(guild_id: int, clan_id: int) -> bool:
    t_clans = _guild.guild_table(guild_id, "honey_clans")
    t_members = _guild.guild_table(guild_id, "honey_clan_members")
    async with _connect() as conn:
        cur = await conn.execute(f"DELETE FROM {t_clans} WHERE id = ?", (int(clan_id),))
        await conn.execute(f"DELETE FROM {t_members} WHERE clan_id = ?", (int(clan_id),))
        await conn.commit()
    return cur.rowcount > 0


async def he_clan_member_add(guild_id: int, clan_id: int, user_discord_id: int) -> bool:
    table = _guild.guild_table(guild_id, "honey_clan_members")
    try:
        async with _connect() as conn:
            await conn.execute(
                f"INSERT INTO {table} (clan_id, user_discord_id, joined_at) VALUES (?, ?, ?)",
                (int(clan_id), int(user_discord_id), _now()))
            await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def he_clan_member_remove(guild_id: int, clan_id: int, user_discord_id: int) -> bool:
    table = _guild.guild_table(guild_id, "honey_clan_members")
    async with _connect() as conn:
        cur = await conn.execute(
            f"DELETE FROM {table} WHERE clan_id = ? AND user_discord_id = ?",
            (int(clan_id), int(user_discord_id)))
        await conn.commit()
    return cur.rowcount > 0


async def he_clan_members(guild_id: int, clan_id: int) -> list[dict]:
    t_members = _guild.guild_table(guild_id, "honey_clan_members")
    async with _connect() as conn:
        async with conn.execute(
            f"SELECT m.user_discord_id, m.joined_at, u.username, u.display_name "
            f"FROM {t_members} m LEFT JOIN users u "
            f"ON u.discord_id = m.user_discord_id WHERE m.clan_id = ? ORDER BY m.id",
            (int(clan_id),)) as cur:
            rows = await cur.fetchall()
    return [{"user_discord_id": int(r[0]), "joined_at": r[1],
             "username": r[2], "display_name": r[3]} for r in rows]


# ============================================================
# Рамки аватара (#27 статичные / #45 GIF — финальный этап)
# ============================================================

async def he_frames_catalog(guild_id: int, user_discord_id: int) -> dict:
    """Каталог рамок + купленные пользователем (типы frame / gif_frame)."""
    owned_frame = await _db.g_cosmetics_owned(guild_id, user_discord_id, "frame")
    owned_gif = await _db.g_cosmetics_owned(guild_id, user_discord_id, "gif_frame")
    frames = [{"key": k, "name": v, "cost": FRAME_COST, "owned": k in owned_frame}
              for k, v in FRAMES.items()]
    gifs = [{"key": k, "name": v, "cost": GIF_FRAME_COST, "owned": k in owned_gif}
            for k, v in GIF_FRAMES.items()]
    return {"frames": frames, "gif_frames": gifs,
            "owned": {"frame": owned_frame, "gif_frame": owned_gif}}


async def he_frame_buy(guild_id: int, user_discord_id: int, frame_key: str,
                       gif: bool = False) -> dict:
    """Купить рамку через generic g_cosmetic_buy(type=frame/gif_frame)."""
    cosmetic_type = "gif_frame" if gif else "frame"
    catalog = GIF_FRAMES if gif else FRAMES
    if frame_key not in catalog:
        return {"ok": False, "error": "unknown_frame"}
    cost = GIF_FRAME_COST if gif else FRAME_COST
    return await _db.g_cosmetic_buy(guild_id, user_discord_id, cosmetic_type,
                                    frame_key, cost)
