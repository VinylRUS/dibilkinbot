"""FastAPI APIRouter — маршруты колеса (wheel).

Извлечено из web.py при рефакторинге (Task ID: refactor-routes).
Содержит:
- HTML-страницу /wheel
- REST API /api/wheel/* (items, spin, result, clear, load-from-watchlist)
- Режим на выбывание /api/wheel/spin_elimination
- Тестовый режим колеса /api/wheel/test/* (только для Матки)
- WebSocket /ws/wheel (real-time обновления)

Общие зависимости (auth, templates, anonymize_items) импортируются из deps.py —
без circular imports.
"""
from __future__ import annotations

import asyncio
import math
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import crypto
import db
import wheel_test_log
import ws_manager
from config import settings

from deps import (
    anonymize_items,
    get_current_guild_id,
    require_admin,
    require_superuser,
    require_user,
    templates,
)

router = APIRouter()


# === Module-level state (v2.3.3) ===
# Lock против race condition при одновременных спинах.
# Ключ — guild_id. Не даёт двум юзерам одновременно крутить колесо в одной гильдии,
# что могло привести к удалению одного и того же item'а дважды и рассинхрону клиентов.
_wheel_spin_locks: dict[int, asyncio.Lock] = {}

# Вариант D: состояние активных спинов.
# spin_id → {guild_id, items, mode, is_test, duration, started_at, completed}
# Используется /api/wheel/result для валидации и fallback-таймером.
_active_spins: dict[str, dict] = {}


# === Wheel HTML page ===

@router.get("/wheel", response_class=HTMLResponse)
async def wheel_page(request: Request, _user: dict = Depends(require_user)):
    """Страница с Canvas-анимацией колеса.

    Передаём в шаблон:
    - items: список лотов (анонимизированный)
    - last_collection: последний завершённый сбор (для баннера «Колесо загружено из сбора»)
    - active_collection: активный сбор (если есть — предупреждаем)
    - recent_winner: последний победитель за час (для блокировки повторной крутки)
    """
    guild_id = get_current_guild_id(_user)
    raw_items = await db.g_list_wheel_items(guild_id, active_only=True)
    items = anonymize_items(raw_items)

    # Инфо о сборах для баннера
    last_collection = await db.g_get_last_completed_collection(guild_id)
    active_collection = await db.g_get_active_collection(guild_id)

    # Недавний победитель (за последний час) — для блокировки повторной крутки
    recent_winner = await db.g_get_recent_winner(guild_id, since_minutes=60)

    # v2.3.3: статус тестового режима (для UI — бейдж + кнопки Матки)
    is_test_mode = await _is_wheel_test_mode(int(guild_id))

    return templates.TemplateResponse(request, "wheel.html", {
        "user": _user,
        "items": items,
        "current_guild_id": guild_id,
        "last_collection": last_collection,
        "active_collection": active_collection,
        "recent_winner": recent_winner,
        "wheel_count": len(items),
        "is_test_mode": is_test_mode,
    })


@router.get("/api/wheel/items")
async def api_wheel_items(_user: dict = Depends(require_user)):
    """Получить текущие лоты колеса (JSON). Анонимно — added_by не возвращается."""
    guild_id = get_current_guild_id(_user)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    # Убираем added_by и added_at из ответа — анонимность
    # (имя добавившего раскрывается только при победе)
    safe_items = [
        {"id": item["id"], "name": item["name"], "tmdb_id": item["tmdb_id"], "color": item["color"]}
        for item in items
    ]
    return JSONResponse({"items": safe_items, "count": len(safe_items)})


# === Wheel API ===

@router.post("/api/wheel/items")
async def api_add_wheel_item(
    request: Request,
    _user: dict = Depends(require_user),
    name: str = Form(...),
    tmdb_id: int | None = Form(None),
):
    """Добавить лот в колесо. Триггерит broadcast всем WS-клиентам."""
    name = name.strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    guild_id = get_current_guild_id(_user)
    discord_id = _user.get("discord_id", 0)
    item_id = await db.g_add_wheel_item(guild_id, name, tmdb_id, discord_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(anonymize_items(items))
    return JSONResponse({"id": item_id, "items": anonymize_items(items), "count": len(items)})


@router.delete("/api/wheel/items/{item_id}")
async def api_remove_wheel_item(
    item_id: int,
    _user: dict = Depends(require_user),
):
    """Удалить лот (soft-delete). Триггерит broadcast."""
    guild_id = get_current_guild_id(_user)
    removed = await db.g_remove_wheel_item(guild_id, item_id)
    if not removed:
        return JSONResponse({"error": "not found"}, status_code=404)
    await db.g_reassign_colors(guild_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(anonymize_items(items))
    return JSONResponse({"removed": True, "items": anonymize_items(items), "count": len(items)})


@router.post("/api/wheel/clear")
async def api_clear_wheel(_user: dict = Depends(require_admin)):
    """Очистить всё колесо (только админ)."""
    guild_id = get_current_guild_id(_user)
    count = await db.g_clear_wheel(guild_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(anonymize_items(items))
    return JSONResponse({"cleared": count, "items": items})


@router.post("/api/wheel/load-from-watchlist")
async def api_load_from_watchlist(_user: dict = Depends(require_user)):
    """Загрузить в колесо все непросмотренные фильмы из списков желаемого всех юзеров.
    Пропускает фильмы, уже просмотренные (в watched) и уже в колесе.
    """
    guild_id = get_current_guild_id(_user)
    # init_guild_tables уже вызывается при on_ready бота

    # Все непросмотренные фильмы из списков желаемого
    all_films = await db.g_get_all_unwatched_watchlist(guild_id)

    # Текущие лоты колеса (чтобы не дублировать)
    current_items = await db.g_list_wheel_items(guild_id, active_only=True)
    existing_titles = {item["name"].lower() for item in current_items}

    added = 0
    for film in all_films:
        title = film["title"]
        # Пропускаем дубликаты
        if title.lower() in existing_titles:
            continue
        # Пропускаем просмотренные
        if await db.g_is_watched(guild_id, title):
            continue
        # Добавляем в колесо
        await db.g_add_wheel_item(guild_id, title, film.get("tmdb_id"), film["user_discord_id"])
        existing_titles.add(title.lower())
        added += 1

    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(anonymize_items(items))
    return JSONResponse({"loaded": added, "total": len(items)})


@router.post("/api/wheel/spin")
async def api_spin_wheel(_user: dict = Depends(require_user), duration: int = Form(5),
                          client_spin_id: str = Form("")):
    """Запустить спин (классический режим).

    v2.3.3 Вариант D — настоящее торможение + серверный валидатор:
    Backend НЕ выбирает winner заранее. Шлёт WS spin_started без target_item.
    Frontend крутит по физике (импульс + трение), останавливается сам,
    отправляет POST /api/wheel/result с winner_id + rotation.
    Backend валидирует и постит.
    Fallback: если за 30с+duration результат не пришёл — backend сам
    выбирает winner через random.choice (на случай закрытой вкладки).
    """
    import random
    guild_id = get_current_guild_id(_user)

    is_test = await _is_wheel_test_mode(int(guild_id))

    async def _list_items():
        if is_test:
            return await db.g_list_test_wheel_items(int(guild_id))
        return await db.g_list_wheel_items(guild_id, active_only=True)

    lock = _wheel_spin_locks.setdefault(int(guild_id), asyncio.Lock())
    if lock.locked():
        return JSONResponse({"error": "Колесо уже крутится, дождитесь результата.", "error_code": "already_spinning"}, status_code=409)
    async with lock:
        # Lock освобождается сразу после создания spin_state — флаг completed
        # в _active_spins[spin_id] защищает от повторных result-запросов.
        # Fallback-таймер проверяет completed флаг, не lock.
        active_collection = await db.g_get_active_collection(guild_id)
        if active_collection and not is_test:
            return JSONResponse({
                "error": "Идёт активный сбор фильмов. Сначала завершите его на странице /movienight — выбранные фильмы загрузятся в колесо.",
                "error_code": "active_collection_exists",
            }, status_code=400)

        if not is_test:
            recent_winner = await db.g_get_recent_winner(guild_id, since_minutes=60)
            if recent_winner:
                items_check = await db.g_list_wheel_items(guild_id, active_only=True)
                if len(items_check) < 2:
                    return JSONResponse({
                        "error": f"Победитель уже определён: «{recent_winner['lot_name']}». Повторная крутка невозможна — начните новый сбор на /movienight.",
                        "error_code": "winner_already_determined",
                        "winner": recent_winner,
                    }, status_code=400)

        items = await _list_items()
        if len(items) < 2:
            return JSONResponse({"error": "need at least 2 items to spin"}, status_code=400)

        spin_id = client_spin_id.strip() if client_spin_id.strip() else str(uuid.uuid4())[:8]
        # Сохраняем состояние активного спина для /api/wheel/result
        _active_spins[spin_id] = {
            "guild_id": int(guild_id),
            "items": items,
            "mode": "classic",
            "is_test": is_test,
            "duration": duration,
            "started_at": asyncio.get_event_loop().time(),
            "completed": False,
        }

        if is_test:
            wheel_test_log.log_event("spin_started", {
                "mode": "classic", "spin_id": spin_id,
                "items_before": [{"id": it["id"], "name": it["name"]} for it in items],
                "duration_sec": duration,
            })

        # Шлём WS spin_started БЕЗ target_item — frontend крутит по физике
        await ws_manager.broadcast_spin_started(
            items, spin_id,
            target_item=None,
            mode="classic",
            winner=None,
            eliminated=None,
            duration=duration,
        )

        # Fallback: если за duration+30с результат не пришёл — сами выберем
        asyncio.create_task(_spin_fallback(spin_id, mode="classic"))

        return JSONResponse({
            "spin_id": spin_id,
            "items": anonymize_items(items),
            "duration": duration,
            "mode": "classic",
            "is_test": is_test,
        })


def _get_sector_under_pointer(rotation: float, items_count: int) -> int:
    """Вычислить индекс сектора под указателем для данного rotation.

    Указатель вверху (12 часов) = угол -π/2 в canvas системе.
    Сектор i находится на углах [i*anglePer, (i+1)*anglePer] в локальных координатах.
    После поворота на rotation сектор под указателем — тот, для которого
    (i*anglePer + rotation) ≡ -π/2 (mod 2π).

    Возвращает индекс сектора или -1 если items_count == 0.
    """
    if items_count == 0:
        return -1
    angle_per = (2 * math.pi) / items_count
    norm = (-math.pi / 2 - rotation) % (2 * math.pi)
    if norm < 0:
        norm += 2 * math.pi
    return int(norm // angle_per) % items_count


@router.post("/api/wheel/result")
async def api_wheel_result(
    _user: dict = Depends(require_user),
    spin_id: str = Form(...),
    winner_id: int = Form(...),
    rotation: float = Form(...),
    mode: str = Form("classic"),
):
    """Frontend сообщает где остановилось колесо.

    v2.3.3 Вариант D: frontend крутит по физике, останавливается сам,
    определяет сектор под указателем и шлёт сюда winner_id + rotation.
    Backend валидирует: пересчитывает сектор по rotation, сверяет с winner_id.
    Если совпадает — записывает, удаляет из колеса, постит в Discord/TG.
    Если не совпадает — использует winner_id от frontend (доверяем) но логируем.
    """
    import math as _math
    import random

    spin_state = _active_spins.get(spin_id)
    if not spin_state:
        return JSONResponse({"error": "Спин не найден или уже завершён", "error_code": "spin_not_found"}, status_code=404)
    if spin_state["completed"]:
        return JSONResponse({"error": "Спин уже завершён", "error_code": "already_completed"}, status_code=409)

    guild_id = spin_state["guild_id"]
    items = spin_state["items"]
    is_test = spin_state["is_test"]
    expected_mode = spin_state["mode"]

    # Валидация: найти сектор под указателем по rotation
    actual_idx = _get_sector_under_pointer(rotation, len(items))
    actual_item = items[actual_idx] if 0 <= actual_idx < len(items) else None
    validation_ok = actual_item and actual_item["id"] == winner_id

    if not validation_ok:
        # Аномалия: frontend сообщил winner_id, но по rotation другой сектор.
        # Доверяем frontend (возможно округление float), но логируем.
        import logging
        logging.getLogger("wheel").warning(
            "Spin %s: rotation mismatch. frontend winner_id=%s, rotation=%.4f → sector idx=%s (item id=%s). Using frontend value.",
            spin_id, winner_id, rotation, actual_idx,
            actual_item["id"] if actual_item else None,
        )
        if is_test:
            wheel_test_log.log_event("validation_mismatch", {
                "spin_id": spin_id, "frontend_winner_id": winner_id,
                "rotation": rotation, "actual_idx": actual_idx,
                "actual_item_id": actual_item["id"] if actual_item else None,
            })

    # Находим item по winner_id (в elimination это — выбывающий, не победитель)
    eliminated = next((it for it in items if it["id"] == winner_id), None)
    if not eliminated:
        return JSONResponse({"error": "winner_id не найден в списке", "error_code": "winner_not_found"}, status_code=400)

    # Помечаем как завершённый — другие result-запросы проигнорируются
    spin_state["completed"] = True

    # Удаляем "winner" (в classic — победитель, в elimination — выбывающий) из колеса
    remove_fn = db.g_remove_test_wheel_item if is_test else db.g_remove_wheel_item
    await remove_fn(guild_id, eliminated["id"])
    if not is_test:
        await db.g_reassign_colors(guild_id)

    # v2.3.3 Вариант D: режимы
    # classic: winner_id = победитель → записываем в winners, постинг
    # elimination: winner_id = выбывающий → НЕ записываем в winners, НЕ постим
    # elimination_final: winner_id = выбывающий → удаляем победителя тоже,
    #   победителя записываем в winners, постим
    actual_winner = None  # кто записывается в winners и постится в Discord/TG
    if mode == "classic":
        actual_winner = eliminated
        await db.g_add_winner(guild_id, str(actual_winner["id"]), actual_winner["name"],
                              actual_winner.get("tmdb_id"), "confirmed", is_test=is_test)
        if not is_test:
            await db.g_mark_watchlist_watched(guild_id, actual_winner["name"])
    elif mode == "elimination_final":
        # 2 элемента: eliminated выбывает, winner = второй (оставшийся)
        actual_winner = next((it for it in items if it["id"] != eliminated["id"]), None)
        if actual_winner:
            # Удаляем победителя из колеса тоже (колесо пустое после финала)
            await remove_fn(guild_id, actual_winner["id"])
            if not is_test:
                await db.g_reassign_colors(guild_id)
            # Записываем победителя в winners
            await db.g_add_winner(guild_id, str(actual_winner["id"]), actual_winner["name"],
                                  actual_winner.get("tmdb_id"), "confirmed", is_test=is_test)
            if not is_test:
                await db.g_mark_watchlist_watched(guild_id, actual_winner["name"])
    # mode == "elimination" — ничего не записываем в winners, просто удалили выбывшего

    if is_test:
        wheel_test_log.log_event("spin_completed", {
            "spin_id": spin_id, "mode": mode,
            "eliminated": {"id": eliminated["id"], "name": eliminated["name"]},
            "winner": {"id": actual_winner["id"], "name": actual_winner["name"]} if actual_winner else None,
            "rotation": rotation, "validation_ok": validation_ok,
        })

    # Шлём WS spin_completed с rotation — другие клиенты выставят эту позицию
    # В elimination — "winner" = выбывающий (для баннера "выбыл X")
    # В classic/final — "winner" = победитель
    broadcast_winner = actual_winner if actual_winner else eliminated
    await ws_manager.broadcast("spin_completed", {
        "spin_id": spin_id,
        "winner": anonymize_items([broadcast_winner])[0] if broadcast_winner else None,
        "eliminated": anonymize_items([eliminated])[0] if mode in ("elimination", "elimination_final") else None,
        "rotation": rotation,
        "mode": mode,
        "remaining_count": max(0, len(items) - (2 if mode == "elimination_final" else 1)),
    })

    # Постинг в Discord/TG — только если есть actual_winner (classic или elimination_final)
    if actual_winner:
        try:
            import bot as bot_module
            bot = bot_module.get_bot_instance()
            if bot:
                await bot.on_wheel_spin_completed(actual_winner, is_test=is_test)
        except Exception as e:
            import logging
            logging.getLogger("ws_manager").warning("Discord notify failed: %s", e)

    # Через 1с — обновить список лотов у всех клиентов (winner удалён)
    async def _delayed_wheel_update():
        await asyncio.sleep(1.0)
        if is_test:
            remaining = await db.g_list_test_wheel_items(guild_id)
        else:
            remaining = await db.g_list_wheel_items(guild_id, active_only=True)
        await ws_manager.broadcast_wheel_updated(anonymize_items(remaining))
        # Чистим состояние
        _active_spins.pop(spin_id, None)
    asyncio.create_task(_delayed_wheel_update())

    return JSONResponse({
        "ok": True,
        "spin_id": spin_id,
        "eliminated": eliminated,
        "winner": actual_winner,  # None для elimination (промежуточный раунд)
        "rotation": rotation,
        "validation_ok": validation_ok,
    })


async def _spin_fallback(spin_id: str, mode: str = "classic") -> None:
    """Если frontend не прислал /api/wheel/result за duration+30с —
    backend сам выбирает (на случай закрытой вкладки).

    В classic: выбирает победителя, удаляет, записывает в winners.
    В elimination: выбирает выбывающего, удаляет, НЕ записывает в winners.
    В elimination_final: выбирает выбывающего, удаляет обоих, победителя в winners.
    """
    import random
    spin_state = _active_spins.get(spin_id)
    if not spin_state:
        return
    if spin_state["completed"]:
        return

    duration = spin_state.get("duration", 5)
    wait_sec = max(1, min(int(duration), 30)) + 30
    await asyncio.sleep(wait_sec)

    spin_state = _active_spins.get(spin_id)
    if not spin_state or spin_state["completed"]:
        return

    guild_id = spin_state["guild_id"]
    items = spin_state["items"]
    is_test = spin_state["is_test"]

    # Backend сам выбирает (аналог frontend: сектор под случайным rotation)
    eliminated = random.choice(items)
    spin_state["completed"] = True

    import logging
    logging.getLogger("wheel").warning(
        "Spin %s: fallback activated — frontend didn't report result in %ss. Picking eliminated=%s (mode=%s)",
        spin_id, wait_sec, eliminated["name"], mode,
    )

    # Удаляем eliminated из колеса
    remove_fn = db.g_remove_test_wheel_item if is_test else db.g_remove_wheel_item
    await remove_fn(guild_id, eliminated["id"])
    if not is_test:
        await db.g_reassign_colors(guild_id)

    # Определяем actual_winner по режиму
    actual_winner = None
    if mode == "classic":
        actual_winner = eliminated
        await db.g_add_winner(guild_id, str(actual_winner["id"]), actual_winner["name"],
                              actual_winner.get("tmdb_id"), "confirmed", is_test=is_test)
        if not is_test:
            await db.g_mark_watchlist_watched(guild_id, actual_winner["name"])
    elif mode == "elimination_final":
        actual_winner = next((it for it in items if it["id"] != eliminated["id"]), None)
        if actual_winner:
            await remove_fn(guild_id, actual_winner["id"])
            if not is_test:
                await db.g_reassign_colors(guild_id)
            await db.g_add_winner(guild_id, str(actual_winner["id"]), actual_winner["name"],
                                  actual_winner.get("tmdb_id"), "confirmed", is_test=is_test)
            if not is_test:
                await db.g_mark_watchlist_watched(guild_id, actual_winner["name"])
    # mode == "elimination" — ничего в winners

    if is_test:
        wheel_test_log.log_event("fallback", {
            "spin_id": spin_id, "mode": mode,
            "eliminated": {"id": eliminated["id"], "name": eliminated["name"]},
            "winner": {"id": actual_winner["id"], "name": actual_winner["name"]} if actual_winner else None,
            "reason": "frontend_timeout",
        })

    # Шлём WS
    broadcast_winner = actual_winner if actual_winner else eliminated
    await ws_manager.broadcast("spin_completed", {
        "spin_id": spin_id,
        "winner": anonymize_items([broadcast_winner])[0] if broadcast_winner else None,
        "eliminated": anonymize_items([eliminated])[0] if mode in ("elimination", "elimination_final") else None,
        "rotation": None,  # fallback — нет точной позиции
        "mode": mode,
        "remaining_count": max(0, len(items) - (2 if mode == "elimination_final" else 1)),
        "fallback": True,
    })

    # Постинг — только если есть actual_winner
    if actual_winner:
        try:
            import bot as bot_module
            bot = bot_module.get_bot_instance()
            if bot:
                await bot.on_wheel_spin_completed(actual_winner, is_test=is_test)
        except Exception as e:
            logging.getLogger("ws_manager").warning("Discord notify failed: %s", e)

    # Обновить список + освободить lock
    async def _cleanup():
        await asyncio.sleep(1.0)
        if is_test:
            remaining = await db.g_list_test_wheel_items(guild_id)
        else:
            remaining = await db.g_list_wheel_items(guild_id, active_only=True)
        await ws_manager.broadcast_wheel_updated(anonymize_items(remaining))
        _active_spins.pop(spin_id, None)
    asyncio.create_task(_cleanup())


async def _delayed_spin_result(winner: dict, remaining: list[dict], spin_id: str,
                                is_test: bool = False, delay_sec: float = 5.0,
                                mode: str = "classic",
                                eliminated: dict | None = None) -> None:
    """Через delay_sec секунд разослать финальный результат спина + постинг в Discord/TG.

    v2.3.3: НЕ слать автоматически wheel_updated — он ломает анимацию на других клиентах
    если spinDuration > 5с. Frontend сам вызовет fetchItems() после завершения анимации.

    v2.3.3: delay_sec = длительность анимации + 0.5с буфер.
    Пост в Discord/TG идёт ПОСЛЕ завершения анимации (Вариант C).

    v2.3.3: mode + eliminated — для elimination_final чтобы другие клиенты
    показали правильный баннер «выбыл X / победитель Y».
    """
    await asyncio.sleep(delay_sec)
    await ws_manager.broadcast_spin_result(winner, spin_id, mode=mode, eliminated=eliminated)

    # Уведомить Discord через бота (в тестовом режиме — с префиксом [TEST])
    try:
        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if bot:
            await bot.on_wheel_spin_completed(winner, is_test=is_test)
    except Exception as e:
        import logging
        logging.getLogger("ws_manager").warning("Discord notify failed: %s", e)
        if is_test:
            wheel_test_log.log_event("discord_notify_error", {"error": str(e)})


@router.post("/api/wheel/spin_elimination")
async def api_spin_wheel_elimination(_user: dict = Depends(require_user), duration: int = Form(5),
                                       client_spin_id: str = Form("")):
    """Режим 'на выбывание' — Вариант D.

    v2.3.3 Вариант D: как /api/wheel/spin, но mode='elimination' или 'elimination_final'.
    Backend НЕ выбирает winner/eliminated заранее. Шлёт WS spin_started без target_item.
    Frontend крутит по физике, определяет eliminated/winner по позиции, шлёт /api/wheel/result.
    Fallback через duration+30с.
    """
    import random
    guild_id = get_current_guild_id(_user)

    is_test = await _is_wheel_test_mode(int(guild_id))

    async def _list_items():
        if is_test:
            return await db.g_list_test_wheel_items(int(guild_id))
        return await db.g_list_wheel_items(guild_id, active_only=True)

    lock = _wheel_spin_locks.setdefault(int(guild_id), asyncio.Lock())
    if lock.locked():
        return JSONResponse({"error": "Колесо уже крутится, дождитесь результата.", "error_code": "already_spinning"}, status_code=409)
    async with lock:
        active_collection = await db.g_get_active_collection(guild_id)
        if active_collection and not is_test:
            return JSONResponse({
                "error": "Идёт активный сбор фильмов. Сначала завершите его на странице /movienight — выбранные фильмы загрузятся в колесо.",
                "error_code": "active_collection_exists",
            }, status_code=400)

        if not is_test:
            recent_winner = await db.g_get_recent_winner(guild_id, since_minutes=60)
            if recent_winner:
                items_check = await db.g_list_wheel_items(guild_id, active_only=True)
                if len(items_check) < 2:
                    return JSONResponse({
                        "error": f"Победитель уже определён: «{recent_winner['lot_name']}». Повторная крутка невозможна — начните новый сбор на /movienight.",
                        "error_code": "winner_already_determined",
                        "winner": recent_winner,
                    }, status_code=400)

        items = await _list_items()
        if len(items) < 2:
            return JSONResponse({"error": "need at least 2 items"}, status_code=400)

        spin_id = client_spin_id.strip() if client_spin_id.strip() else str(uuid.uuid4())[:8]

        # v2.3.3 Вариант D: НЕ выбираем winner/eliminated заранее.
        # mode = 'elimination_final' если 2 элемента (финал), иначе 'elimination'.
        mode = "elimination_final" if len(items) == 2 else "elimination"

        _active_spins[spin_id] = {
            "guild_id": int(guild_id),
            "items": items,
            "mode": mode,
            "is_test": is_test,
            "duration": duration,
            "started_at": asyncio.get_event_loop().time(),
            "completed": False,
        }

        if is_test:
            wheel_test_log.log_event("spin_started", {
                "mode": mode, "spin_id": spin_id,
                "items_before": [{"id": it["id"], "name": it["name"]} for it in items],
                "duration_sec": duration,
            })

        # Шлём WS spin_started БЕЗ target_item
        await ws_manager.broadcast_spin_started(
            items, spin_id,
            target_item=None,
            mode=mode,
            winner=None,
            eliminated=None,
            duration=duration,
        )

        # Fallback
        asyncio.create_task(_spin_fallback(spin_id, mode=mode))

        return JSONResponse({
            "spin_id": spin_id,
            "items": anonymize_items(items),
            "duration": duration,
            "mode": mode,
            "is_test": is_test,
        })


async def _delayed_elimination_result(eliminated: dict, remaining: list[dict], spin_id: str, delay_sec: float = 5.0) -> None:
    """Через delay_sec секунд разослать результат elimination-спина (кого удалили).

    v2.3.3: НЕ слать автоматически wheel_updated — он ломает анимацию на других клиентах
    (если spinDuration > 5с, сектора скачут в середине анимации). Frontend сам вызовет
    fetchItems() после завершения анимации и обновит список.
    Только шлём elimination_result event для уведомления других клиентов.

    v2.3.3: delay_sec = длительность анимации + 0.5с буфер.
    """
    await asyncio.sleep(delay_sec)
    await ws_manager.broadcast(
        "elimination_result",
        {"eliminated": anonymize_items([eliminated])[0] if eliminated else None,
         "remaining_count": len(remaining)},
    )


# === v2.3.3: Тестовый режим колеса (только для Матки) ===

@router.post("/api/wheel/test/start")
async def api_wheel_test_start(_user: dict = Depends(require_superuser)):
    """Запустить тестовый режим колеса.

    - Создаёт лог-файл для записи событий
    - Все последующие спины помечают items/winners как is_test=1
    - Постинг в Discord/TG идёт с префиксом [TEST]
    """
    admin_id = _user.get("discord_id", 0)
    admin_name = _user.get("username", f"User{admin_id}")
    guild_id = int(get_current_guild_id(_user))
    try:
        log_path = wheel_test_log.start_test_log(admin_id, admin_name, guild_id)
        await db.set_setting("wheel_test_mode", "1", is_secret=False)
        await db.set_setting("wheel_test_admin_id", str(admin_id), is_secret=False)
        await db.set_setting("wheel_test_guild_id", str(guild_id), is_secret=False)
        wheel_test_log.log_event("test_mode_started", {
            "admin_id": admin_id, "admin_name": admin_name, "guild_id": guild_id,
        })
        return JSONResponse({"ok": True, "log_path": log_path})
    except Exception as e:
        import logging
        logging.getLogger("wheel_test").error("Failed to start test mode: %s", e, exc_info=True)
        return JSONResponse({"error": f"Не удалось запустить тестовый режим: {e}"}, status_code=500)


@router.post("/api/wheel/test/end")
async def api_wheel_test_end(_user: dict = Depends(require_superuser)):
    """Завершить тестовый режим.

    - Удаляет ВСЕ тестовые записи (wheel_items, winners, ratings) из БД
    - Закрывает лог-файл
    - Сбрасывает флаг wheel_test_mode
    """
    guild_id = int(get_current_guild_id(_user))
    try:
        # Статистика до очистки
        stats_before = await db.g_get_test_stats(guild_id)
        wheel_test_log.log_event("cleanup_started", {"stats_before": stats_before})

        # Удаляем тестовые данные
        cleanup_stats = await db.g_purge_test_data(guild_id)
        wheel_test_log.log_event("cleanup_completed", {"stats": cleanup_stats})

        # Закрываем лог и сбрасываем флаги
        log_path = wheel_test_log.end_test_log(cleanup_stats=cleanup_stats)
        await db.set_setting("wheel_test_mode", "0", is_secret=False)
        await db.delete_setting("wheel_test_admin_id")
        await db.delete_setting("wheel_test_guild_id")

        # Уведомить WS клиентов — список колеса обновился (тестовые items удалены)
        items = await db.g_list_wheel_items(guild_id, active_only=True)
        await ws_manager.broadcast_wheel_updated(anonymize_items(items))

        return JSONResponse({
            "ok": True,
            "log_path": log_path,
            "cleanup_stats": cleanup_stats,
        })
    except Exception as e:
        import logging
        logging.getLogger("wheel_test").error("Failed to end test mode: %s", e, exc_info=True)
        return JSONResponse({"error": f"Не удалось завершить тестовый режим: {e}"}, status_code=500)


@router.get("/api/wheel/test/status")
async def api_wheel_test_status(_user: dict = Depends(require_superuser)):
    """Текущий статус тестового режима."""
    is_active = (await db.get_setting("wheel_test_mode")) == "1"
    admin_id = await db.get_setting("wheel_test_admin_id")
    guild_id = await db.get_setting("wheel_test_guild_id")
    stats = {}
    log_path = wheel_test_log.get_current_log_path()
    if is_active and guild_id:
        try:
            stats = await db.g_get_test_stats(int(guild_id))
        except Exception:
            pass
    return JSONResponse({
        "active": is_active,
        "admin_id": int(admin_id) if admin_id and admin_id.isdigit() else None,
        "guild_id": int(guild_id) if guild_id and guild_id.isdigit() else None,
        "log_path": log_path,
        "stats": stats,
    })


@router.post("/api/wheel/test/add_item")
async def api_wheel_test_add_item(_user: dict = Depends(require_superuser), name: str = Form(...)):
    """Добавить тестовый item в колесо (для ручной проверки flow)."""
    guild_id = int(get_current_guild_id(_user))
    admin_id = _user.get("discord_id", 0)
    if (await db.get_setting("wheel_test_mode")) != "1":
        return JSONResponse({"error": "Тестовый режим не активен"}, status_code=400)
    try:
        item_id = await db.g_add_test_wheel_item(guild_id, name.strip(), None, admin_id)
        items = await db.g_list_test_wheel_items(guild_id)
        await ws_manager.broadcast_wheel_updated(anonymize_items(items))
        wheel_test_log.log_event("item_added", {"item_id": item_id, "name": name.strip()})
        return JSONResponse({"ok": True, "item_id": item_id, "items": anonymize_items(items), "count": len(items)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/wheel/test/logs")
async def api_wheel_test_logs(_user: dict = Depends(require_superuser)):
    """Список всех лог-файлов тестов."""
    files = wheel_test_log.list_log_files()
    return JSONResponse({"files": files})


@router.get("/api/wheel/test/logs/{filename}")
async def api_wheel_test_log_content(filename: str, _user: dict = Depends(require_superuser)):
    """Прочитать содержимое лог-файла."""
    content = wheel_test_log.read_log_file(filename)
    if content is None:
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    return JSONResponse({"filename": filename, "content": content})


# === Helper: проверка тестового режима ===

async def _is_wheel_test_mode(guild_id: int) -> bool:
    """True если тестовый режим колеса активен для этой гильдии."""
    if (await db.get_setting("wheel_test_mode")) != "1":
        return False
    test_guild = await db.get_setting("wheel_test_guild_id")
    if not test_guild or not test_guild.isdigit():
        return False
    return int(test_guild) == int(guild_id)


# === WebSocket: real-time обновления колеса ===

@router.websocket("/ws/wheel")
async def ws_wheel(websocket: WebSocket):
    """WebSocket для подписки на обновления колеса.
    guild_id передаётся как query-параметр: /ws/wheel?guild_id=123
    """
    import json
    # Читаем guild_id из query-параметров (по умолчанию 0)
    guild_id_str = websocket.query_params.get("guild_id", "0")
    try:
        guild_id = int(guild_id_str)
    except (ValueError, TypeError):
        guild_id = 0

    await ws_manager.connect(websocket)
    try:
        # При первом подключении сразу шлём текущее состояние (анонимизированное)
        items = await db.g_list_wheel_items(guild_id, active_only=True)
        safe_items = anonymize_items(items)
        await websocket.send_text(json.dumps({
            "type": "wheel_updated",
            "payload": {"items": safe_items, "count": len(safe_items)},
        }, ensure_ascii=False))
        # Держим соединение, ждём сообщений (клиент может слать ping)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        import logging
        logging.getLogger("ws_manager").debug("WS error: %s", e)
        await ws_manager.disconnect(websocket)
