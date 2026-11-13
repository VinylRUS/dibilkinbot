"""v3.11: тесты экономики «Мёда» 🍯.

Проверяем: заработок выводится из существующих таблиц (watched/ratings/quotes/
user_achievements), траты/бонусы идут через honey_ledger, баланс не уходит в минус,
лидерборд и стрик активности считаются корректно.

Паттерн как в conftest: прямые вставки в guild-таблицы тестовой гильдии.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import aiosqlite
import pytest

from conftest import TEST_GUILD_ID, run_async

HONEY_USER = 90001  # уникальный id, не пересекается с тестовой Маткой (1001)


async def _seed():
    """Заполнить данные пользователя HONEY_USER и очистить предыдущие прогоны."""
    import db
    from config import settings
    from guild import guild_table

    t_watched = guild_table(TEST_GUILD_ID, "watched")
    t_ratings = guild_table(TEST_GUILD_ID, "ratings")
    t_quotes = guild_table(TEST_GUILD_ID, "quotes")
    t_uach = guild_table(TEST_GUILD_ID, "user_achievements")
    t_ledger = guild_table(TEST_GUILD_ID, "honey_ledger")
    t_act = guild_table(TEST_GUILD_ID, "activity_log")
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(settings.database_path) as c:
        # Идемпотентность: чистим всё, что относится к HONEY_USER
        await c.execute(f"DELETE FROM {t_watched} WHERE watcher_user_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_ratings} WHERE user_discord_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_quotes} WHERE recorded_by = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_uach} WHERE user_discord_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_ledger} WHERE user_discord_id = ?", (HONEY_USER,))
        await c.execute(f"DELETE FROM {t_act} WHERE actor_discord_id = ?", (HONEY_USER,))

        # 2 просмотра → 2 * 5 = 10
        for i in range(2):
            await c.execute(
                f"INSERT INTO {t_watched} (title, watched_at, watcher_user_id) VALUES (?, ?, ?)",
                (f"Film {i}", now, HONEY_USER))
        # 3 оценки → 3 * 3 = 9 (distinct winner_id из-за UNIQUE)
        for wid in range(1, 4):
            await c.execute(
                f"INSERT INTO {t_ratings} (winner_id, user_discord_id, rating, created_at) "
                f"VALUES (?, ?, ?, ?)", (wid, HONEY_USER, 7.0, now))
        # 1 цитата → 1 * 2 = 2
        await c.execute(
            f"INSERT INTO {t_quotes} (author, text, recorded_by, recorded_at) VALUES (?, ?, ?, ?)",
            ("Someone", "quote text", HONEY_USER, now))
        # 1 активная ачивка → 1 * 25 = 25
        await c.execute(
            f"INSERT INTO {t_uach} (achievement_id, user_discord_id, granted_at, is_active) "
            f"VALUES (?, ?, ?, 1)", (1, HONEY_USER, now))
        await c.commit()
    # Итого earned = 10 + 9 + 2 + 25 = 46
    return 46


@pytest.mark.asyncio
async def test_honey_earned_balance(client):
    import db
    expected = await _seed()

    bd = await db.g_honey_breakdown(TEST_GUILD_ID, HONEY_USER)
    assert bd["watched"] == 2
    assert bd["ratings"] == 3
    assert bd["quotes"] == 1
    assert bd["achievements"] == 1
    assert bd["earned"] == expected
    assert bd["adjustments"] == 0
    assert bd["balance"] == expected
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == expected


@pytest.mark.asyncio
async def test_honey_spend_and_grant(client):
    import db
    earned = await _seed()  # 46

    # Трата больше баланса — отказ
    assert await db.g_honey_spend(TEST_GUILD_ID, HONEY_USER, earned + 1, "too much") is False
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned

    # Валидная трата
    assert await db.g_honey_spend(TEST_GUILD_ID, HONEY_USER, 20, "accent color") is True
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned - 20

    # amount <= 0 — отказ, баланс не меняется
    assert await db.g_honey_spend(TEST_GUILD_ID, HONEY_USER, 0, "noop") is False
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned - 20

    # Ручной бонус
    await db.g_honey_grant(TEST_GUILD_ID, HONEY_USER, 10, "event prize", granted_by=1001)
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == earned - 20 + 10


@pytest.mark.asyncio
async def test_honey_balance_never_negative(client):
    import db
    await _seed()
    # Админ списывает больше, чем заработано (штраф) — баланс клампится в 0
    await db.g_honey_grant(TEST_GUILD_ID, HONEY_USER, -9999, "big penalty", granted_by=1001)
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == 0


@pytest.mark.asyncio
async def test_honey_leaderboard(client):
    import db
    earned = await _seed()
    board = await db.g_honey_leaderboard(TEST_GUILD_ID, limit=10)
    entry = next((r for r in board if r["user_discord_id"] == HONEY_USER), None)
    assert entry is not None
    assert entry["balance"] == earned


def test_dashboard_renders_honey(client):
    """Дашборд отдаёт 200 и содержит блок «Мёд» (контекст honey прокинут в шаблон)."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Мёд" in r.text
    assert "honey-board" in r.text


@pytest.mark.asyncio
async def test_controversial_films(client):
    """«Спорные фильмы»: фильм с большим разбросом оценок ранжируется выше."""
    import db
    from config import settings
    from guild import guild_table

    t_win = guild_table(TEST_GUILD_ID, "winners")
    t_rat = guild_table(TEST_GUILD_ID, "ratings")
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(settings.database_path) as c:
        cur = await c.execute(
            f"INSERT INTO {t_win} (lot_name, confidence, detected_at) VALUES (?, ?, ?)",
            ("Спорный", "high", now))
        wid_spread = cur.lastrowid
        cur = await c.execute(
            f"INSERT INTO {t_win} (lot_name, confidence, detected_at) VALUES (?, ?, ?)",
            ("Единодушный", "high", now))
        wid_tight = cur.lastrowid
        # Большой разброс: 1,10,1,10 (σ=4.5)
        for i, val in enumerate((1.0, 10.0, 1.0, 10.0)):
            await c.execute(
                f"INSERT INTO {t_rat} (winner_id, user_discord_id, rating, created_at) "
                f"VALUES (?, ?, ?, ?)", (wid_spread, 80000 + i, val, now))
        # Единодушие: 7,7,8,7 (σ≈0.43)
        for i, val in enumerate((7.0, 7.0, 8.0, 7.0)):
            await c.execute(
                f"INSERT INTO {t_rat} (winner_id, user_discord_id, rating, created_at) "
                f"VALUES (?, ?, ?, ?)", (wid_tight, 81000 + i, val, now))
        await c.commit()

    rows = await db.g_controversial_films(TEST_GUILD_ID, min_ratings=3, limit=5)
    assert len(rows) == 2
    assert rows[0]["winner_id"] == wid_spread  # спорный — первый
    assert rows[0]["stddev"] > rows[1]["stddev"]
    assert rows[0]["count"] == 4


@pytest.mark.asyncio
async def test_recommendations(client):
    """«Вам может понравиться»: фильм, высоко оценённый пользователем с похожим
    вкусом, рекомендуется; низкий порог оценок у цели → пусто."""
    import db
    from config import settings
    from guild import guild_table

    t_win = guild_table(TEST_GUILD_ID, "winners")
    t_rat = guild_table(TEST_GUILD_ID, "ratings")
    now = datetime.utcnow().isoformat()
    T = 82001   # цель
    S = 82002   # похожий вкус

    async with aiosqlite.connect(settings.database_path) as c:
        wids = []
        for name in ("A", "B", "C", "D"):
            cur = await c.execute(
                f"INSERT INTO {t_win} (lot_name, confidence, detected_at) VALUES (?, ?, ?)",
                (name, "high", now))
            wids.append(cur.lastrowid)
        wa, wb, wc, wd = wids
        # T оценил A,B,C (>= 3 — хватает затравки), не видел D
        for w, r in ((wa, 9.0), (wb, 8.0), (wc, 9.0)):
            await c.execute(f"INSERT INTO {t_rat} (winner_id, user_discord_id, rating, created_at) "
                            f"VALUES (?, ?, ?, ?)", (w, T, r, now))
        # S — почти идентичные оценки на общих + высоко оценил D
        for w, r in ((wa, 9.0), (wb, 8.0), (wd, 10.0)):
            await c.execute(f"INSERT INTO {t_rat} (winner_id, user_discord_id, rating, created_at) "
                            f"VALUES (?, ?, ?, ?)", (w, S, r, now))
        await c.commit()

    recs = await db.g_recommend_films(TEST_GUILD_ID, T, limit=5)
    rec_ids = [r["winner_id"] for r in recs]
    assert wd in rec_ids  # D рекомендован (похожий вкус оценил высоко)
    top = next(r for r in recs if r["winner_id"] == wd)
    assert top["score"] >= 9.0

    # Пользователь без затравки (< 3 оценок) — рекомендаций нет
    assert await db.g_recommend_films(TEST_GUILD_ID, 999999, limit=5) == []


@pytest.mark.asyncio
async def test_follow_and_feed(client):
    """Follow/unfollow, счётчики и лента активности подписок."""
    import db
    from config import settings
    from guild import guild_table

    A, B = 83001, 83002  # A подписывается на B
    # follow
    assert await db.g_follow(TEST_GUILD_ID, A, A) is False  # нельзя на себя
    assert await db.g_follow(TEST_GUILD_ID, A, B) is True
    assert await db.g_is_following(TEST_GUILD_ID, A, B) is True
    counts_b = await db.g_follow_counts(TEST_GUILD_ID, B)
    assert counts_b["followers"] == 1
    counts_a = await db.g_follow_counts(TEST_GUILD_ID, A)
    assert counts_a["following"] == 1

    # B совершает действие → появляется в ленте A
    await db.g_log_activity(TEST_GUILD_ID, "quote_added", B, actor_name="Bee", target_name="цитата")
    feed = await db.g_following_feed(TEST_GUILD_ID, A, limit=10)
    assert any(ev["actor_discord_id"] == B for ev in feed)

    # unfollow
    await db.g_unfollow(TEST_GUILD_ID, A, B)
    assert await db.g_is_following(TEST_GUILD_ID, A, B) is False
    assert (await db.g_following_feed(TEST_GUILD_ID, A, limit=10)) == []

    # Чистим activity_log B, чтобы не влиять на другие тесты
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(
            f"DELETE FROM {guild_table(TEST_GUILD_ID, 'activity_log')} WHERE actor_discord_id = ?",
            (B,))
        await c.commit()


@pytest.mark.asyncio
async def test_santa_anonymous_chat(client):
    """Анонимный чат: сообщения санты и получателя в одном треде, порядок и роли;
    обратный поиск назначения по получателю даёт санту (внутренне)."""
    import db
    from config import settings
    from guild import guild_table

    S, R = 91001, 91002  # санта, получатель
    ev = await db.g_create_santa_event(TEST_GUILD_ID, "НГ", "2099-01-01T00:00:00", "до 1000₽", 1001)
    eid = ev["id"]
    # Назначение напрямую (минуя требование 4+ участников)
    async with aiosqlite.connect(settings.database_path) as c:
        await c.execute(
            f"INSERT INTO {guild_table(TEST_GUILD_ID, 'santa_assignments')} "
            f"(event_id, santa_discord_id, recipient_discord_id, assigned_at) VALUES (?, ?, ?, ?)",
            (eid, S, R, datetime.utcnow().isoformat()))
        await c.commit()

    # Обратный поиск: по получателю → его санта (внутренняя функция)
    rec = await db.g_santa_recipient_assignment(TEST_GUILD_ID, eid, R)
    assert rec and rec["santa_discord_id"] == S

    # Переписка
    assert await db.g_santa_add_message(TEST_GUILD_ID, eid, S, R, "santa", "Привет! Что хочешь?") is True
    assert await db.g_santa_add_message(TEST_GUILD_ID, eid, S, R, "recipient", "Инди-игры") is True
    assert await db.g_santa_add_message(TEST_GUILD_ID, eid, S, R, "santa", "   ") is False  # пусто
    assert await db.g_santa_add_message(TEST_GUILD_ID, eid, S, R, "bogus", "x") is False  # роль

    msgs = await db.g_santa_get_messages(TEST_GUILD_ID, eid, S, R)
    assert [m["sender"] for m in msgs] == ["santa", "recipient"]
    assert msgs[0]["text"] == "Привет! Что хочешь?"


def test_profile_renders_honey(client):
    """Публичный профиль отдаёт 200 и показывает карточку «Мёд»."""
    r = client.get("/u/1001")  # TEST_ADMIN_DISCORD_ID из conftest
    assert r.status_code == 200
    assert "Мёд" in r.text


@pytest.mark.asyncio
async def test_cosmetic_buy(client):
    """Покупка accent-цвета списывает мёд один раз; повторно — бесплатно; без мёда — отказ."""
    import db
    earned = await _seed()  # 46 мёда, чистит леджер пользователя

    # Недостаточно (стоимость 150 > 46)
    res = await db.g_cosmetic_buy(TEST_GUILD_ID, HONEY_USER, "accent", "rose", 150)
    assert res["ok"] is False and res["error"] == "insufficient"
    assert await db.g_cosmetic_is_owned(TEST_GUILD_ID, HONEY_USER, "accent", "rose") is False

    # Дарим мёда и покупаем
    await db.g_honey_grant(TEST_GUILD_ID, HONEY_USER, 200, "test top-up")
    bal_before = await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER)
    res = await db.g_cosmetic_buy(TEST_GUILD_ID, HONEY_USER, "accent", "rose", 150)
    assert res["ok"] is True and res["owned"] is True
    assert res["balance"] == bal_before - 150
    assert await db.g_cosmetic_is_owned(TEST_GUILD_ID, HONEY_USER, "accent", "rose") is True
    assert "rose" in await db.g_cosmetics_owned(TEST_GUILD_ID, HONEY_USER, "accent")

    # Повторная покупка того же — бесплатно (баланс не меняется)
    bal2 = await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER)
    res = await db.g_cosmetic_buy(TEST_GUILD_ID, HONEY_USER, "accent", "rose", 150)
    assert res["ok"] is True
    assert await db.g_honey_balance(TEST_GUILD_ID, HONEY_USER) == bal2


@pytest.mark.asyncio
async def test_tournament_flow(client):
    """Полный цикл: создание 4-фильмового турнира → голосование → продвижение → чемпион."""
    import db
    from config import settings
    from guild import guild_table

    # Чистим турнирные таблицы (autouse-фикстура их не трогает)
    async with aiosqlite.connect(settings.database_path) as c:
        for base in ("tournaments", "tournament_matches", "tournament_votes"):
            await c.execute(f"DELETE FROM {guild_table(TEST_GUILD_ID, base)}")
        await c.commit()

    tid = await db.g_create_tournament(TEST_GUILD_ID, "Кубок", ["A", "B", "C", "D"], 1001)
    assert tid is not None
    r1 = await db.g_get_tournament_matches(TEST_GUILD_ID, tid, 1)
    assert len(r1) == 2  # (A vs B), (C vs D)

    m_ab, m_cd = r1[0], r1[1]
    # В паре A/B: B (right) получает 2 голоса, A (left) — 1
    for u in (70001, 70002):
        assert (await db.g_vote_match(TEST_GUILD_ID, m_ab["id"], u, "right"))["ok"]
    assert (await db.g_vote_match(TEST_GUILD_ID, m_ab["id"], 70003, "left"))["ok"]
    # В паре C/D: C (left) получает 2 голоса
    for u in (70001, 70002):
        assert (await db.g_vote_match(TEST_GUILD_ID, m_cd["id"], u, "left"))["ok"]

    # Перезапись своего голоса не увеличивает счётчик
    res = await db.g_vote_match(TEST_GUILD_ID, m_ab["id"], 70001, "right")
    assert res["right_votes"] == 2

    adv = await db.g_advance_tournament(TEST_GUILD_ID, tid)
    assert adv["ok"] and adv["status"] == "voting" and adv["round"] == 2
    r2 = await db.g_get_tournament_matches(TEST_GUILD_ID, tid, 2)
    assert len(r2) == 1
    final = r2[0]
    assert {final["left_name"], final["right_name"]} == {"B", "C"}  # победители раунда 1

    # В финале побеждает C
    c_side = "left" if final["left_name"] == "C" else "right"
    assert (await db.g_vote_match(TEST_GUILD_ID, final["id"], 70001, c_side))["ok"]
    adv2 = await db.g_advance_tournament(TEST_GUILD_ID, tid)
    assert adv2["ok"] and adv2["status"] == "completed" and adv2["winner"] == "C"

    tour = await db.g_get_tournament(TEST_GUILD_ID, tid)
    assert tour["status"] == "completed" and tour["winner_name"] == "C"


@pytest.mark.asyncio
async def test_tournament_needs_two(client):
    """Турнир из < 2 уникальных фильмов не создаётся."""
    import db
    assert await db.g_create_tournament(TEST_GUILD_ID, "T", ["A", "A", "  "], 1001) is None


def test_tournament_page_renders(client):
    r = client.get("/tournament")
    assert r.status_code == 200
    assert "Турнир" in r.text


def test_pwa_routes(client):
    """PWA: /sw.js из корня с правильным scope-заголовком и /manifest.webmanifest."""
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert "javascript" in sw.headers.get("content-type", "")
    assert sw.headers.get("Service-Worker-Allowed") == "/"
    assert "serviceWorker" not in sw.text  # это сам воркер, не регистрация

    man = client.get("/manifest.webmanifest")
    assert man.status_code == 200
    data = man.json()
    assert data["name"] == "DeeBeelkin"
    assert data["display"] == "standalone"
    assert any(i["sizes"] == "512x512" for i in data["icons"])


@pytest.mark.asyncio
async def test_activity_streak(client):
    import db
    from config import settings
    from guild import guild_table

    await _seed()  # _seed чистит activity_log пользователя
    t_act = guild_table(TEST_GUILD_ID, "activity_log")
    today = datetime.utcnow().date()
    async with aiosqlite.connect(settings.database_path) as c:
        for d in (today, today - timedelta(days=1), today - timedelta(days=2)):
            await c.execute(
                f"INSERT INTO {t_act} (event_type, actor_discord_id, actor_name, created_at) "
                f"VALUES (?, ?, ?, ?)",
                ("login", HONEY_USER, "tester", d.isoformat() + "T12:00:00"))
        await c.commit()
    # 3 дня подряд, заканчивая сегодня
    assert await db.g_activity_streak(TEST_GUILD_ID, HONEY_USER) == 3
