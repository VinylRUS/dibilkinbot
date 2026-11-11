"""Интеграционные тесты колеса — Фаза 0 плана (ветка feature/v3.6-wheel-tests).

Покрывают (по HANDOFF_wheel_audit.md, раздел 4):
- CRUD лотов: POST /api/wheel/items, DELETE, POST /api/wheel/clear, переназначение цветов
- Классический спин: победитель == items[physics.winner_idx], удаляется из колеса,
  записывается в winners с confidence='confirmed'
- Спин на выбывание: elimination-раунд (одного выбывает, в winners ничего не пишется),
  elimination_final (побеждает ДРУГОЙ элемент — не тот, что под указателем),
  колесо пустеет после финала
- Конкуренция: 409 already_spinning во время спина; lock снимается после завершения
- WS: spin_started/spin_completed обоим клиентам; recovery опоздавшего клиента
  (подключение во время SPINNING); close 4001 для не-approved guild_id
- /api/wheel/state — polling fallback (items + active_spin)
- Инвариант физики: winner_idx == get_sector_under_pointer(final_rotation, items_count)

Известный баг 2.1 (lock НЕ снимается при исключении внутри _delayed_complete_spin)
покрыт тестом test_lock_recovery_on_exception с маркером xfail(strict=False):
сейчас он ожидаемо падает; после фикса в Фазе 2 начнёт проходить — XPASS
не роняет прогон, но сигналит, что фикс пора перепроверить в тесте.

Запуск из корня репо:
    python -m pytest tests/test_wheel_integration.py -v
"""
from __future__ import annotations

import time

import pytest
from starlette.websockets import WebSocketDisconnect

import db
from conftest import TEST_GUILD_ID, run_async
from wheel_physics import generate_spin, get_sector_under_pointer


# === Helpers ===

def add_items(client, names):
    """Добавить лоты через API. Падает с телом ответа в сообщении assert."""
    for n in names:
        r = client.post("/api/wheel/items", data={"name": n})
        assert r.status_code == 200, (
            f"POST /api/wheel/items({n!r}) -> {r.status_code}: {r.text}")


def spin(client, duration=1, elimination=False):
    """Запустить спин. Возвращает JSON-тело ответа (spin_id, items, physics...)."""
    url = "/api/wheel/spin_elimination" if elimination else "/api/wheel/spin"
    r = client.post(url, data={"duration": duration})
    assert r.status_code == 200, f"POST {url} -> {r.status_code}: {r.text}"
    return r.json()


def current_items(client):
    r = client.get("/api/wheel/items")
    assert r.status_code == 200, f"GET /api/wheel/items -> {r.status_code}: {r.text}"
    return r.json()["items"]


def wait_spin_done(client, timeout=15.0):
    """Polling GET /api/wheel/state до ПОЛНОГО завершения спина.

    Замена time.sleep из ранней версии тестов (HANDOFF, п.2).

    Два условия готовности (обязательны оба):
    1. active_spin is None — НО этого мало: status="COMPLETED" ставится
       в НАЧАЛЕ _delayed_complete_spin (wheel_routes.py:508), а _active_spins.pop
       и lock.release() — в самом КОНЦЕ (590-598). Между ними окно в сотни мс
       (записи в БД + sleep(0.3)), в которое следующий спин ловит ложный 409.
    2. guild-lock снят — читаем wheel_routes._wheel_spin_locks (module-level
       dict, уже используется фикстурой изоляции). release происходит последним,
       поэтому его снятие гарантирует: winner записан, items удалены, pop сделан.

    С lock-маркером гонки «SPINNING→409» между спинами одного теста исчезают.
    """
    import wheel_routes

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        r = client.get("/api/wheel/state")
        assert r.status_code == 200, f"GET /api/wheel/state -> {r.status_code}: {r.text}"
        last = r.json()
        if last.get("active_spin") is None:
            lock = wheel_routes._wheel_spin_locks.get(TEST_GUILD_ID)
            if lock is None or not lock.locked():
                return last
        time.sleep(0.05)
    raise AssertionError(f"Спин не завершился за {timeout}s; последнее состояние: {last}")


# === Items CRUD ===

def test_items_crud_and_colors(client):
    add_items(client, ["One", "Two", "Three"])
    items = current_items(client)
    assert [it["name"] for it in items] == ["One", "Two", "Three"]
    # Цвета назначаются по порядку добавления из WHEEL_COLORS
    assert [it["color"] for it in items] == db.WHEEL_COLORS[:3]
    # Анонимизация: added_by / added_at не должны выходить наружу
    assert set(items[0].keys()) == {"id", "name", "tmdb_id", "color"}

    # Удаление → soft-delete + переназначение цветов
    victim = items[0]
    r = client.delete(f"/api/wheel/items/{victim['id']}")
    assert r.status_code == 200, f"DELETE -> {r.status_code}: {r.text}"
    items2 = current_items(client)
    assert victim["id"] not in {it["id"] for it in items2}
    assert [it["color"] for it in items2] == db.WHEEL_COLORS[:2]

    # Очистка (require_admin — сессия суперпользователя проходит)
    r = client.post("/api/wheel/clear")
    assert r.status_code == 200, f"POST /api/wheel/clear -> {r.status_code}: {r.text}"
    assert current_items(client) == []


def test_wheel_state_endpoint(client):
    add_items(client, ["S1", "S2"])
    r = client.get("/api/wheel/state")
    assert r.status_code == 200, f"GET /api/wheel/state -> {r.status_code}: {r.text}"
    body = r.json()
    assert body["count"] == 2
    assert body["active_spin"] is None
    assert body["is_test"] is False
    assert all(set(it.keys()) == {"id", "name", "tmdb_id", "color"} for it in body["items"])


# === Классический спин ===

def test_classic_spin(client):
    names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
    add_items(client, names)

    body = spin(client, duration=1)
    items = body["items"]                 # порядок лотов на момент спина
    winner_idx = body["physics"]["winner_idx"]
    assert 0 <= winner_idx < len(items), (
        f"winner_idx={winner_idx} вне диапазона (len={len(items)})")
    expected_winner = items[winner_idx]

    wait_spin_done(client)

    # Победитель удалён из колеса, остальные на месте
    remaining = current_items(client)
    assert expected_winner["id"] not in {it["id"] for it in remaining}
    assert len(remaining) == len(names) - 1

    # Победитель записан в winners как confirmed
    winners = run_async(db.g_list_winners(TEST_GUILD_ID))
    assert len(winners) == 1, f"ожидали 1 winner, получили: {winners}"
    assert winners[0][1] == expected_winner["name"]  # lot_name
    assert winners[0][3] == "confirmed"              # confidence


# === Спин на выбывание ===

def test_elimination_to_final(client):
    add_items(client, ["Ф1", "Ф2", "Ф3"])

    # Раунд 1: 3 лота → mode 'elimination', выбывает items[winner_idx],
    # в winners НИЧЕГО не пишется
    b1 = spin(client, duration=1, elimination=True)
    assert b1["mode"] == "elimination"
    round1_eliminated = b1["items"][b1["physics"]["winner_idx"]]

    wait_spin_done(client)
    remaining = current_items(client)
    assert round1_eliminated["id"] not in {it["id"] for it in remaining}
    assert len(remaining) == 2
    assert run_async(db.g_list_winners(TEST_GUILD_ID)) == []

    # Раунд 2: 2 лота → финал. Побеждает ДРУГОЙ элемент (не под указателем)
    b2 = spin(client, duration=1, elimination=True)
    assert b2["mode"] == "elimination_final"
    final_items = b2["items"]
    final_eliminated = final_items[b2["physics"]["winner_idx"]]
    expected_champion = next(
        it for it in final_items if it["id"] != final_eliminated["id"])

    wait_spin_done(client)

    # После финала колесо пустое, чемпион записан в winners
    assert current_items(client) == []
    winners = run_async(db.g_list_winners(TEST_GUILD_ID))
    assert len(winners) == 1
    assert winners[0][1] == expected_champion["name"]
    assert winners[0][3] == "confirmed"


def test_winner_idx_matches_items_order(client):
    """Инвариант (HANDOFF, п.5): индекс из physics указывает на реально
    выбывающий/победивший элемент в порядке items, в обоих режимах."""
    # classic: побеждает items[winner_idx]
    add_items(client, ["K1", "K2", "K3", "K4"])
    b = spin(client, duration=1)
    expected = b["items"][b["physics"]["winner_idx"]]
    wait_spin_done(client)
    remaining_ids = {it["id"] for it in current_items(client)}
    assert expected["id"] not in remaining_ids
    winners = run_async(db.g_list_winners(TEST_GUILD_ID))
    assert winners[0][1] == expected["name"]

    # elimination: выбывает items[winner_idx] (победитель не записывается)
    add_items(client, ["E1", "E2", "E3", "E4"])
    b2 = spin(client, duration=1, elimination=True)
    eliminated = b2["items"][b2["physics"]["winner_idx"]]
    wait_spin_done(client)
    remaining_ids = {it["id"] for it in current_items(client)}
    assert eliminated["id"] not in remaining_ids


# === Конкурирующие спины ===

def test_concurrent_spin_rejected(client):
    add_items(client, ["A", "B", "C", "D", "E"])

    spin(client, duration=3)

    r = client.post("/api/wheel/spin", data={"duration": 1})
    assert r.status_code == 409, f"ожидали 409, получили {r.status_code}: {r.text}"
    assert r.json()["error_code"] == "already_spinning"

    wait_spin_done(client, timeout=15)

    # Lock снят — новый спин проходит
    spin(client, duration=1)
    wait_spin_done(client)
    assert len(current_items(client)) == 3  # 5 - два победителя


@pytest.mark.xfail(
    reason="wheel bug 2.1: lock остаётся занятым при исключении внутри "
           "_delayed_complete_spin (нет try/finally). Фикс — Фаза 2. "
           "После фикса тест начнёт проходить (strict=False: XPASS не роняет прогон).",
    strict=False,
)
def test_lock_recovery_on_exception(client, monkeypatch):
    """Документирует СЛЕЛУЮЩЕЕ (правильное) поведение: после падения
    фоновой задачи завершения спина guild-lock должен освобождаться —
    иначе каждый следующий спин 409 до рестарта процесса (симптом
    «ничего не работает» из HANDOFF, п.2.1)."""
    import db as db_mod

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated failure in g_add_winner")

    monkeypatch.setattr(db_mod, "g_add_winner", boom)

    add_items(client, ["X1", "X2", "X3", "X4"])
    spin(client, duration=1)
    time.sleep(2.5)  # задача успела дойти до падения (duration + 0.5 + запас)

    r = client.post("/api/wheel/spin", data={"duration": 1})
    assert r.status_code == 200, (
        f"lock не освободился после исключения: {r.status_code}: {r.text}")


# === WebSocket ===

def test_ws_two_clients_spin_events(client):
    add_items(client, ["Movie A", "Movie B", "Movie C"])
    ws_url = f"/ws/wheel?guild_id={TEST_GUILD_ID}"

    with client.websocket_connect(ws_url) as ws1:
        # При подключении — начальное состояние колеса
        init1 = ws1.receive_json()
        assert init1["type"] == "wheel_updated"
        assert init1["payload"]["count"] == 3

        body = spin(client, duration=2)
        winner_idx = body["physics"]["winner_idx"]
        winner_name = body["items"][winner_idx]["name"]

        # Подключённый заранее клиент получает spin_started
        started1 = ws1.receive_json()
        assert started1["type"] == "spin_started"
        assert started1["payload"]["spin_id"] == body["spin_id"]
        assert started1["payload"]["mode"] == "classic"
        assert started1["payload"]["physics"]["winner_idx"] == winner_idx

        # Опоздавший клиент подключается ВО ВРЕМЯ SPINNING → recovery:
        # начальное состояние + активный спин (HANDOFF, п.4)
        with client.websocket_connect(ws_url) as ws2:
            init2 = ws2.receive_json()
            assert init2["type"] == "wheel_updated"
            started2 = ws2.receive_json()
            assert started2["type"] == "spin_started"
            assert started2["payload"]["spin_id"] == body["spin_id"]
            assert started2["payload"]["physics"]["winner_idx"] == winner_idx

            completed2 = ws2.receive_json()
            assert completed2["type"] == "spin_completed"
            assert completed2["payload"]["winner"]["name"] == winner_name

        completed1 = ws1.receive_json()
        assert completed1["type"] == "spin_completed"
        assert completed1["payload"]["spin_id"] == body["spin_id"]
        assert completed1["payload"]["winner"]["name"] == winner_name

        # После завершения — обновлённый список лотов (победитель удалён)
        updated1 = ws1.receive_json()
        assert updated1["type"] == "wheel_updated"
        assert updated1["payload"]["count"] == 2

    state = client.get("/api/wheel/state").json()
    assert state["count"] == 2


def test_ws_invalid_guild_rejected(client):
    """Не-approved guild_id → error + close 4001 (без бесконечного реконнекта)."""
    with client.websocket_connect("/ws/wheel?guild_id=424242") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["payload"]["code"] == "invalid_guild"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# === Физика (юнит, без HTTP) ===

def test_physics_winner_idx_consistency():
    """winner_idx из generate_spin всегда совпадает с сектором под указателем
    при final_rotation — тот же расчёт использует и сервер, и фронтенд."""
    for items_count in range(2, 13):
        for _ in range(10):
            ph = generate_spin(
                spin_id="unit", items_count=items_count,
                current_rotation=0.0, duration_sec=1.0, mode="classic",
            )
            assert 0 <= ph.winner_idx < items_count
            assert ph.winner_idx == get_sector_under_pointer(ph.final_rotation, items_count)
            assert ph.final_rotation == ph.initial_rotation + ph.total_rotation
