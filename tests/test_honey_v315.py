"""v3.15: тесты экономики «Мёда» 🍯 — банк, подарки, ставки, лотерея,
пчела, дивиденды, гильдии, уровни, Steam-бонус, dedupe_key.

Паттерн как в test_honey.py: прямые вставки/вызовы по тестовой гильдии
из conftest. Каждый тест использует уникальные user-id, чистит за собой.
"""
from __future__ import annotations

import aiosqlite
from datetime import datetime, timedelta

import pytest

from conftest import TEST_GUILD_ID, run_async

# Уникальные юзеры, не пересекаются с тестами v3.11 (90001) и Маткой (1001)
U = {
    "alice": 91001,
    "bob": 91002,
    "carol": 91003,
    "dave": 91004,
}


async def _purge():
    """Идемпотентная очистка honey-таблиц тестовой гильдии."""
    import guild as guild_mod
    from config import settings

    bases = ["honey_bank", "honey_bank_log", "honey_lottery_rounds",
             "honey_lottery_tickets", "honey_bets", "honey_bee_rounds",
             "honey_bee_claims", "honey_clans", "honey_clan_members"]
    async with aiosqlite.connect(settings.database_path) as c:
        for base in bases:
            t = guild_mod.guild_table(TEST_GUILD_ID, base)
            await c.execute(f"DELETE FROM {t}")
        # чистим леджер только наших тестовых юзеров
        t_ledger = guild_mod.guild_table(TEST_GUILD_ID, "honey_ledger")
        for uid in U.values():
            await c.execute(f"DELETE FROM {t_ledger} WHERE user_discord_id = ?", (uid,))
        t_act = guild_mod.guild_table(TEST_GUILD_ID, "activity_log")
        for uid in U.values():
            await c.execute(f"DELETE FROM {t_act} WHERE actor_discord_id = ?", (uid,))
        await c.commit()


async def _fund(uid: int, amount: int):
    """Начислить мёд тестовому юзеру напрямую в леджер (без dedupe)."""
    import honey_economy as he
    assert await he._ledger_insert(TEST_GUILD_ID, uid, amount, reason="test_seed")


async def _upsert_users():
    """Юзеры нужны для подарков/гильдий (users-таблица глобальная)."""
    import db as db_mod

    for uid in U.values():
        await db_mod.upsert_user(uid, username=f"u{uid}", display_name=f"U{uid}")


# ===================== dedupe_key: идемпотентность =====================

@pytest.mark.asyncio
async def test_dedupe_grant_idempotent(client):
    import db
    import honey_economy as he
    await _purge()
    uid = U["alice"]
    before = await db.g_honey_balance(TEST_GUILD_ID, uid)
    assert await he.he_grant_dedupe(TEST_GUILD_ID, uid, 30, "steam_link_bonus",
                                    dedupe_key=f"steam_bonus:{uid}")
    assert not await he.he_grant_dedupe(TEST_GUILD_ID, uid, 30, "steam_link_bonus",
                                        dedupe_key=f"steam_bonus:{uid}")
    assert await db.g_honey_balance(TEST_GUILD_ID, uid) == before + 30  # задвоения нет


# ===================== Steam-бонус (#8) =====================

@pytest.mark.asyncio
async def test_steam_bonus_once(client):
    import db
    import honey_economy as he
    await _purge()
    uid = U["alice"]
    before = await db.g_honey_balance(TEST_GUILD_ID, uid)
    assert await he.he_steam_bonus(TEST_GUILD_ID, uid)
    assert not await he.he_steam_bonus(TEST_GUILD_ID, uid)
    assert await db.g_honey_balance(TEST_GUILD_ID, uid) == before + he.HONEY_STEAM_BONUS


# ===================== Подарки + комиссия в банк (#31) =====================

@pytest.mark.asyncio
async def test_gift_commission_to_bank(client):
    import db
    import honey_economy as he
    await _purge()
    await _upsert_users()
    await _fund(U["alice"], 1000)
    bank_before = await he.he_bank_balance(TEST_GUILD_ID)

    res = await he.he_gift(TEST_GUILD_ID, U["alice"], U["bob"], 100)
    assert res["ok"], res
    assert res["delivered"] == 100
    assert res["commission"] == 10  # 10%
    assert await db.g_honey_balance(TEST_GUILD_ID, U["bob"]) == 100
    assert await db.g_honey_balance(TEST_GUILD_ID, U["alice"]) == 1000 - 110
    assert await he.he_bank_balance(TEST_GUILD_ID) == bank_before + 10

    # недостаточно мёда / сам себе / несуществующий получатель
    assert not (await he.he_gift(TEST_GUILD_ID, U["alice"], U["bob"], 10**9))["ok"]
    assert not (await he.he_gift(TEST_GUILD_ID, U["alice"], U["alice"], 10))["ok"]
    assert (await he.he_gift(TEST_GUILD_ID, U["alice"], 777777, 10))["error"] \
        == "recipient_not_found"


# ===================== Уровень пасечника (#52) =====================

@pytest.mark.asyncio
async def test_level_from_total_earned(client):
    import honey_economy as he
    await _purge()
    uid = U["alice"]
    await _fund(uid, 350)  # только положительные delta → total_earned
    lvl = await he.he_level(TEST_GUILD_ID, uid)
    assert lvl["total_earned"] == 350  # earned-часть = 0
    assert lvl["level"] == 3
    assert lvl["progress"] == 50

    # траты НЕ снижают уровень: -100, total_earned не меняется
    import db
    assert await db.g_honey_spend(TEST_GUILD_ID, uid, 100, reason="test_spend")
    lvl2 = await he.he_level(TEST_GUILD_ID, uid)
    assert lvl2["level"] == 3 and lvl2["total_earned"] == 350


# ===================== Лотерея «Улей» (#14) =====================

async def _force_draw_past(guild_id: int):
    """Сдвинуть draw_at текущего открытого раунда в прошлое."""
    import guild as guild_mod
    from config import settings
    from datetime import datetime as _dt

    t = guild_mod.guild_table(guild_id, "honey_lottery_rounds")
    past = (_dt.utcnow() - timedelta(minutes=1)).isoformat()
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(
            f"UPDATE {t} SET draw_at = ? WHERE status = 'open'", (past,))
        await c.commit()


@pytest.mark.asyncio
async def test_lottery_draw_and_prize(client):
    import db
    import honey_economy as he
    await _purge()
    for name in ("alice", "bob", "carol"):
        await _fund(U[name], 1000)
        assert (await he.he_lottery_buy(TEST_GUILD_ID, U[name], qty=2))["ok"]

    info = await he.he_lottery_info(TEST_GUILD_ID, U["alice"])
    assert info["participants"] == 3
    assert info["tickets_total"] == 6
    bank_before = await he.he_bank_balance(TEST_GUILD_ID)
    assert bank_before >= 6 * he.LOTTERY_TICKET_COST  # выручка → банк

    await _force_draw_past(TEST_GUILD_ID)
    result = await he.he_lottery_draw_due(TEST_GUILD_ID)
    assert result and not result["rolled"]
    winner = result["winner"]
    # победителю — 500 (эмиссия, дефолт открытого вопроса)
    assert await db.g_honey_balance(TEST_GUILD_ID, winner) >= \
        1000 - 20 + he.LOTTERY_PRIZE
    # прозрачный RNG публикуется
    assert result["seed"] and 0 <= result["roll"] < result["tickets"]
    # повторный вызов — ничего нового (раунд закрыт)
    assert await he.he_lottery_draw_due(TEST_GUILD_ID) is None


@pytest.mark.asyncio
async def test_lottery_min_participants_refund(client):
    import db
    import honey_economy as he
    await _purge()
    await _fund(U["dave"], 1000)
    assert (await he.he_lottery_buy(TEST_GUILD_ID, U["dave"], qty=3))["ok"]
    bal_before = await db.g_honey_balance(TEST_GUILD_ID, U["dave"])

    await _force_draw_past(TEST_GUILD_ID)
    result = await he.he_lottery_draw_due(TEST_GUILD_ID)
    assert result and result["rolled"]  # 1 участник < минимума 3
    # билеты возвращены: баланс восстановлен, выручка НЕ удваивается
    assert await db.g_honey_balance(TEST_GUILD_ID, U["dave"]) == bal_before + 30


# ===================== Ставки на Колесо (#15) =====================

@pytest.mark.asyncio
async def test_bets_settle_commission_to_bank(client):
    import db
    import honey_economy as he
    await _purge()
    for name in ("alice", "bob", "carol"):
        await _fund(U[name], 1000)

    assert (await he.he_bet_place(TEST_GUILD_ID, U["alice"], "Фильм А", 100))["ok"]
    assert (await he.he_bet_place(TEST_GUILD_ID, U["bob"], "Фильм А", 300))["ok"]
    assert (await he.he_bet_place(TEST_GUILD_ID, U["carol"], "Фильм Б", 60))["ok"]

    open_bets = await he.he_bets_open(TEST_GUILD_ID)
    assert open_bets["pool"] == 460
    assert open_bets["by_item"] == {"Фильм А": 400, "Фильм Б": 60}

    bank_before = await he.he_bank_balance(TEST_GUILD_ID)
    balances = {n: await db.g_honey_balance(TEST_GUILD_ID, U[n])
                for n in ("alice", "bob", "carol")}

    result = await he.he_bets_settle(TEST_GUILD_ID, "Фильм А")
    assert result["bets"] == 3 and result["pool"] == 460
    assert result["commission"] == 46  # 10% → банк
    assert await he.he_bank_balance(TEST_GUILD_ID) == bank_before + 46

    # победители делят 90% пула (414) пропорционально ставке
    payout_pool = 460 - 46
    alice_share = payout_pool * 100 // 400
    bob_share = payout_pool * 300 // 400
    assert await db.g_honey_balance(TEST_GUILD_ID, U["alice"]) == balances["alice"] + alice_share
    assert await db.g_honey_balance(TEST_GUILD_ID, U["bob"]) == balances["bob"] + bob_share
    assert await db.g_honey_balance(TEST_GUILD_ID, U["carol"]) == balances["carol"]  # проиграла

    hist = await he.he_bets_history(TEST_GUILD_ID, U["carol"])
    assert hist and hist[0]["status"] == "lost"


# ===================== «Поймай пчелу» (#16) =====================

@pytest.mark.asyncio
async def test_bee_first_three_atomic(client):
    import honey_economy as he
    await _purge()
    round_id = await he.he_bee_round_create(TEST_GUILD_ID, 111, 222)

    pos_a = await he.he_bee_claim(TEST_GUILD_ID, round_id, U["alice"])
    pos_b = await he.he_bee_claim(TEST_GUILD_ID, round_id, U["bob"])
    pos_c = await he.he_bee_claim(TEST_GUILD_ID, round_id, U["carol"])
    assert (pos_a, pos_b, pos_c) == (1, 2, 3)

    # повторный клейм того же юзера — None
    assert await he.he_bee_claim(TEST_GUILD_ID, round_id, U["alice"]) is None
    # четвёртый — мимо тройки
    assert await he.he_bee_claim(TEST_GUILD_ID, round_id, U["dave"]) is None

    # призы идемпотентны
    assert await he.he_bee_prize(TEST_GUILD_ID, round_id, U["alice"])
    assert not await he.he_bee_prize(TEST_GUILD_ID, round_id, U["alice"])

    claims = await he.he_bee_close_round(TEST_GUILD_ID, round_id)
    assert [c["position"] for c in claims] == [1, 2, 3]
    assert len(claims) == he.HONEY_BEE_WINNERS


# ===================== Дивиденды (#18) =====================

@pytest.mark.asyncio
async def test_dividends_monthly_and_dedupe(client):
    import db
    import guild as guild_mod
    import honey_economy as he
    from config import settings
    await _purge()
    # изолируемся от других тестов: чистая activity_log + банк с нуля
    t_act = guild_mod.guild_table(TEST_GUILD_ID, "activity_log")
    t_bank = guild_mod.guild_table(TEST_GUILD_ID, "honey_bank")
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(f"DELETE FROM {t_act}")
        await c.execute(f"DELETE FROM {t_bank}")
        await c.commit()
    await he.he_bank_deposit(TEST_GUILD_ID, 1000, reason="test deposit")
    now = datetime.utcnow()

    # «активные»: alice и bob с записью activity_log сегодня
    t_act = guild_mod.guild_table(TEST_GUILD_ID, "activity_log")
    async with aiosqlite.connect(settings.database_path) as c:
        for uid in (U["alice"], U["bob"]):
            await c.execute(
                f"INSERT INTO {t_act} (event_type, actor_discord_id, actor_name, created_at) "
                f"VALUES ('login', ?, 'test', ?)", (uid, now.isoformat()))
        await c.commit()

    bal = {n: await db.g_honey_balance(TEST_GUILD_ID, U[n]) for n in ("alice", "bob")}
    res = await he.he_dividends_run(TEST_GUILD_ID, percent=5, now=now)
    assert res["paid"]
    # 5% от 1000 = 50 → по 25 каждому, банк 950
    assert res["per_user"] == 25 and res["users"] == 2
    assert await db.g_honey_balance(TEST_GUILD_ID, U["alice"]) == bal["alice"] + 25
    assert await db.g_honey_balance(TEST_GUILD_ID, U["bob"]) == bal["bob"] + 25
    assert await he.he_bank_balance(TEST_GUILD_ID) == 950

    # повтор в том же месяце — dedupe, банк не трогаем
    res2 = await he.he_dividends_run(TEST_GUILD_ID, percent=5, now=now)
    assert not res2["paid"]
    assert await he.he_bank_balance(TEST_GUILD_ID) == 950


# ===================== Гильдии (#51 → кланы Матки) =====================

@pytest.mark.asyncio
async def test_clans_crud(client):
    import honey_economy as he
    await _purge()
    await _upsert_users()

    clan = await he.he_clan_create(TEST_GUILD_ID, "Улей", "BEE", "Пчелиный клан", U["alice"])
    assert clan and clan["name"] == "Улей" and clan["members"] == 0

    assert await he.he_clan_create(TEST_GUILD_ID, "Улей", None, None, U["alice"]) is None  # дубль имени

    assert await he.he_clan_member_add(TEST_GUILD_ID, clan["id"], U["bob"])
    assert not await he.he_clan_member_add(TEST_GUILD_ID, clan["id"], U["bob"])  # дубль
    members = await he.he_clan_members(TEST_GUILD_ID, clan["id"])
    assert [m["user_discord_id"] for m in members] == [U["bob"]]

    clans = await he.he_clan_list(TEST_GUILD_ID)
    assert [c["name"] for c in clans] == ["Улей"]
    assert clans[0]["members"] == 1

    assert await he.he_clan_member_remove(TEST_GUILD_ID, clan["id"], U["bob"])
    assert await he.he_clan_delete(TEST_GUILD_ID, clan["id"])
    assert await he.he_clan_list(TEST_GUILD_ID) == []


# ===================== Рамки (#27/#45 через generic buy) =====================

@pytest.mark.asyncio
async def test_frames_buy_via_generic(client):
    import db
    import honey_economy as he
    await _purge()
    uid = U["alice"]
    await _fund(uid, 500)

    res = await he.he_frame_buy(TEST_GUILD_ID, uid, "pasechnik", gif=False)
    assert res["ok"], res
    assert await db.g_cosmetics_owned(TEST_GUILD_ID, uid, "frame") == ["pasechnik"]
    # повторная покупка — не списывает (generic идемпотентен)
    res2 = await he.he_frame_buy(TEST_GUILD_ID, uid, "pasechnik", gif=False)
    assert res2["ok"] and res2["owned"]
    assert await db.g_honey_balance(TEST_GUILD_ID, uid) == 500 - he.FRAME_COST

    # GIF-рамка дороже
    res3 = await he.he_frame_buy(TEST_GUILD_ID, uid, "bee_mechanic", gif=True)
    assert res3["ok"], res3
    assert await db.g_honey_balance(TEST_GUILD_ID, uid) == 500 - he.FRAME_COST - he.GIF_FRAME_COST
    assert await db.g_cosmetics_owned(TEST_GUILD_ID, uid, "gif_frame") == ["bee_mechanic"]


# ===================== API: страница и подарки =====================

def test_honey_page_and_gift_api(client):
    import db
    import honey_economy as he
    from conftest import TEST_ADMIN_DISCORD_ID
    run_async(_purge())
    run_async(_upsert_users())
    # отправитель — юзер сессии (тестовая Матка)
    run_async(_fund(TEST_ADMIN_DISCORD_ID, 1000))

    # страница /honey доступна и рендерится
    r = client.get("/honey")
    assert r.status_code == 200
    assert "Мёд" in r.text and "Лотерея" in r.text and "Гильдии" in r.text

    # API подарка
    r = client.post("/api/honey/gift", data={
        "to_discord_id": str(U["bob"]), "amount": "50",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and data["delivered"] == 50 and data["commission"] == 5
    assert run_async(db.g_honey_balance(TEST_GUILD_ID, U["bob"])) == 50
    assert run_async(he.he_bank_balance(TEST_GUILD_ID)) >= 5

    # API уровня
    r = client.get("/api/honey/level")
    assert r.status_code == 200
    lvl = r.json()
    assert lvl["ok"] and "level" in lvl and "progress" in lvl
