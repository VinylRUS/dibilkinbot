"""FastAPI APIRouter — маршруты колеса (wheel).

Извлечено из web.py при рефакторинге (Task ID: refactor-routes).
v2.4.0 — серверная физика колеса.

Содержит:
- HTML-страницу /wheel
- REST API /api/wheel/* (items, spin, clear, load-from-watchlist)
- Режим на выбывание /api/wheel/spin_elimination
- Тестовый режим колеса /api/wheel/test/* (только для Матки)
- WebSocket /ws/wheel (real-time обновления, с восстановлением активного спина)

Архитектура v2.4.0:
- Сервер генерирует параметры физики (wheel_physics.generate_spin) и определяет
  победителя. Frontend только анимирует по этим параметрам.
- Сервер хранит состояние спина (SPINNING → COMPLETED) и удерживает lock
  до полного завершения (duration + buffer).
- Нет endpoint'а /api/wheel/result — сервер сам знает результат.
- WS-клиентам шлётся spin_started с physics, через duration+0.5с — spin_completed.

Общие зависимости (auth, templates, anonymize_items) импортируются из deps.py —
без circular imports.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, Form, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse

import db
import wheel_physics
import wheel_test_log
import ws_manager
from wheel_physics import (
    SpinPhysics,
    generate_spin,
    physics_to_dict,
)

from deps import (
    anonymize_items,
    get_current_guild_id,
    require_admin,
    require_superuser,
    require_user,
    templates,
)

router = APIRouter()

_log = logging.getLogger("wheel")


# === Module-level state ===
# Lock против race condition при одновременных спинах.
# Ключ — guild_id. Не даёт двум юзерам одновременно крутить колесо в одной гильдии.
# v2.4.0: Lock удерживается с момента spin до ПОЛНОГО завершения (COMPLETED),
# а не освобождается сразу после dispatch.
_wheel_spin_locks: dict[int, asyncio.Lock] = {}

# Состояние активных спинов.
# spin_id → {guild_id, items, mode, is_test, duration, started_at, status, physics}
# status: "SPINNING" → "COMPLETED"
# Используется WS-recovery (отправить spin_started опоздавшему клиенту) и
# для блокировки повторных круток (через lock).
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
    """Добавить лот в колесо. Триггерит broadcast всем WS-клиентам гильдии."""
    name = name.strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    guild_id = get_current_guild_id(_user)
    discord_id = _user.get("discord_id", 0)
    item_id = await db.g_add_wheel_item(guild_id, name, tmdb_id, discord_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(int(guild_id), anonymize_items(items))
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
    await ws_manager.broadcast_wheel_updated(int(guild_id), anonymize_items(items))
    return JSONResponse({"removed": True, "items": anonymize_items(items), "count": len(items)})


@router.post("/api/wheel/clear")
async def api_clear_wheel(_user: dict = Depends(require_admin)):
    """Очистить всё колесо (только админ)."""
    guild_id = get_current_guild_id(_user)
    count = await db.g_clear_wheel(guild_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(int(guild_id), anonymize_items(items))
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
    await ws_manager.broadcast_wheel_updated(int(guild_id), anonymize_items(items))
    return JSONResponse({"loaded": added, "total": len(items)})


# === Spin (classic) — серверная физика v2.4.0 ===

@router.post("/api/wheel/spin")
async def api_spin_wheel(_user: dict = Depends(require_user), duration: int = Form(5)):
    """Запустить спин (классический режим).

    v2.4.0 — серверная физика:
    1. Сервер генерирует параметры спина через wheel_physics.generate_spin
       (поворот, конечный угол, winner_idx).
    2. Шлёт WS spin_started с physics — клиенты анимируют по этим параметрам.
    3. Через duration + 0.5с (delayed task) сервер сам определяет winner,
       удаляет из колеса, постит в Discord/TG, шлёт spin_completed.
    4. Lock удерживается до полного завершения (status COMPLETED + cleanup).
    5. Второй запрос во время SPINNING → 409 Conflict.

    Нет endpoint'а /api/wheel/result — серверу не нужно от клиента результат,
    клиент не может повлиять на winner.
    """
    guild_id = int(get_current_guild_id(_user))

    is_test = await _is_wheel_test_mode(guild_id)

    async def _list_items():
        if is_test:
            return await db.g_list_test_wheel_items(guild_id)
        return await db.g_list_wheel_items(guild_id, active_only=True)

    lock = _wheel_spin_locks.setdefault(guild_id, asyncio.Lock())
    if lock.locked():
        return JSONResponse(
            {"error": "Колесо уже крутится, дождитесь результата.",
             "error_code": "already_spinning"},
            status_code=409,
        )

    await lock.acquire()
    success = False
    try:
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

        spin_id = str(uuid.uuid4())[:8]
        physics = generate_spin(
            spin_id=spin_id,
            items_count=len(items),
            current_rotation=0.0,
            duration_sec=duration,
            mode="classic",
            started_at_ts=time.time(),
        )

        _active_spins[spin_id] = {
            "guild_id": guild_id,
            "items": items,
            "mode": "classic",
            "is_test": is_test,
            "duration": duration,
            "started_at": time.time(),
            "status": "SPINNING",
            "physics": physics,
        }

        if is_test:
            wheel_test_log.log_event("spin_started", {
                "mode": "classic",
                "spin_id": spin_id,
                "items_before": [{"id": it["id"], "name": it["name"]} for it in items],
                "duration_sec": duration,
                "winner_idx": physics.winner_idx,
                "final_rotation": physics.final_rotation,
            })

        # Шлём WS spin_started с physics — клиенты анимируют по этим параметрам
        await ws_manager.broadcast_spin_started(
            guild_id, spin_id, physics_to_dict(physics),
            mode="classic",
            items=anonymize_items(items),
            duration=duration,
        )

        # Планируем завершение спина через duration + 0.5с буфер.
        # Эта задача снимет lock после завершения.
        asyncio.create_task(_delayed_complete_spin(spin_id))

        success = True
        return JSONResponse({
            "spin_id": spin_id,
            "items": anonymize_items(items),
            "duration": duration,
            "mode": "classic",
            "is_test": is_test,
            "physics": physics_to_dict(physics),
        })
    finally:
        if not success and lock.locked():
            # Validation failed or exception — освобождаем lock.
            # Success path: lock освобождается в _delayed_complete_spin.
            lock.release()


# === Spin (elimination) — серверная физика v2.4.0 ===

@router.post("/api/wheel/spin_elimination")
async def api_spin_wheel_elimination(_user: dict = Depends(require_user),
                                       duration: int = Form(5)):
    """Режим 'на выбывание' — v2.4.0 серверная физика.

    mode = 'elimination_final' если 2 элемента (финал), иначе 'elimination'.
    Сервер генерирует physics, winner_idx = кто выбывает.
    Для elimination_final: actual_winner = другой (не выбывший) элемент.
    """
    guild_id = int(get_current_guild_id(_user))

    is_test = await _is_wheel_test_mode(guild_id)

    async def _list_items():
        if is_test:
            return await db.g_list_test_wheel_items(guild_id)
        return await db.g_list_wheel_items(guild_id, active_only=True)

    lock = _wheel_spin_locks.setdefault(guild_id, asyncio.Lock())
    if lock.locked():
        return JSONResponse(
            {"error": "Колесо уже крутится, дождитесь результата.",
             "error_code": "already_spinning"},
            status_code=409,
        )

    await lock.acquire()
    success = False
    try:
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

        spin_id = str(uuid.uuid4())[:8]
        # mode = 'elimination_final' если 2 элемента (финал), иначе 'elimination'.
        mode = "elimination_final" if len(items) == 2 else "elimination"

        physics = generate_spin(
            spin_id=spin_id,
            items_count=len(items),
            current_rotation=0.0,
            duration_sec=duration,
            mode=mode,
            started_at_ts=time.time(),
        )

        _active_spins[spin_id] = {
            "guild_id": guild_id,
            "items": items,
            "mode": mode,
            "is_test": is_test,
            "duration": duration,
            "started_at": time.time(),
            "status": "SPINNING",
            "physics": physics,
        }

        if is_test:
            wheel_test_log.log_event("spin_started", {
                "mode": mode,
                "spin_id": spin_id,
                "items_before": [{"id": it["id"], "name": it["name"]} for it in items],
                "duration_sec": duration,
                "winner_idx": physics.winner_idx,
                "final_rotation": physics.final_rotation,
            })

        # Шлём WS spin_started с physics
        await ws_manager.broadcast_spin_started(
            guild_id, spin_id, physics_to_dict(physics),
            mode=mode,
            items=anonymize_items(items),
            duration=duration,
        )

        # Планируем завершение — снимет lock после cleanup
        asyncio.create_task(_delayed_complete_spin(spin_id))

        success = True
        return JSONResponse({
            "spin_id": spin_id,
            "items": anonymize_items(items),
            "duration": duration,
            "mode": mode,
            "is_test": is_test,
            "physics": physics_to_dict(physics),
        })
    finally:
        if not success and lock.locked():
            lock.release()


# === Delayed completion: сервер определяет winner по physics ===

async def _safe_post_to_external(winner: dict, is_test: bool, spin_id: str) -> None:
    """Постинг победителя в Discord + Telegram в фоновой задаче.

    Вынесено из _delayed_complete_spin, чтобы долгие API-вызовы
    (kinopoisk lookup, telegram sendPhoto, discord channel.send)
    не блокировали event loop и WS-broadcast'ы клиентам.

    Любая ошибка логируется, но не роняет процесс.
    """
    try:
        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if not bot:
            _log.debug("Bot instance not available, skipping Discord/TG post")
            return
        await bot.on_wheel_spin_completed(winner, is_test=is_test)
    except Exception as e:
        _log.warning("Discord/TG notify failed for spin %s: %s", spin_id, e)
        if is_test:
            try:
                wheel_test_log.log_event("discord_notify_error", {"error": str(e)})
            except Exception:
                pass


async def _delayed_complete_spin(spin_id: str) -> None:
    """Через duration + 0.5с: определить winner по physics.winner_idx,
    удалить из колеса, добавить в winners (для classic/elimination_final),
    постить в Discord/TG, разослать spin_completed, затем wheel_updated.

    В конце освобождает _wheel_spin_locks[guild_id] (lock удерживался с spin start).

    Идемпотентно: если status уже COMPLETED — выходит без действий.
    """
    spin_state = _active_spins.get(spin_id)
    if not spin_state:
        return
    if spin_state["status"] == "COMPLETED":
        return

    duration = spin_state.get("duration", 5)
    # Ждём пока клиент отанимирует (duration) + небольшой буфер
    await asyncio.sleep(duration + 0.5)

    spin_state = _active_spins.get(spin_id)
    if not spin_state or spin_state["status"] == "COMPLETED":
        return

    guild_id = spin_state["guild_id"]
    items = spin_state["items"]
    is_test = spin_state["is_test"]
    mode = spin_state["mode"]
    physics: SpinPhysics = spin_state["physics"]
    lock = _wheel_spin_locks.get(guild_id)

    # Помечаем завершённым — protects against re-entry
    spin_state["status"] = "COMPLETED"

    winner_idx = physics.winner_idx
    # eliminated — элемент под указателем (в classic: тот же, что и winner;
    # в elimination(_final): выбывающий).
    if 0 <= winner_idx < len(items):
        eliminated = items[winner_idx]
    else:
        _log.warning(
            "Spin %s: winner_idx=%s out of range (len=%s). Falling back to items[0].",
            spin_id, winner_idx, len(items),
        )
        eliminated = items[0]

    # Удаляем eliminated из колеса
    remove_fn = db.g_remove_test_wheel_item if is_test else db.g_remove_wheel_item
    await remove_fn(guild_id, eliminated["id"])
    if not is_test:
        await db.g_reassign_colors(guild_id)

    # Определяем actual_winner — кто записывается в winners и постится в Discord/TG.
    # classic: winner = eliminated (тот же элемент)
    # elimination: actual_winner = None (промежуточный раунд, выбывший просто удаляется)
    # elimination_final: winner = другой элемент (не eliminated)
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
            # Удаляем победителя из колеса тоже (после финала колесо пустое)
            await remove_fn(guild_id, actual_winner["id"])
            if not is_test:
                await db.g_reassign_colors(guild_id)
            await db.g_add_winner(guild_id, str(actual_winner["id"]), actual_winner["name"],
                                  actual_winner.get("tmdb_id"), "confirmed", is_test=is_test)
            if not is_test:
                await db.g_mark_watchlist_watched(guild_id, actual_winner["name"])
    # mode == "elimination" — ничего не записываем в winners

    if is_test:
        wheel_test_log.log_event("spin_completed", {
            "spin_id": spin_id,
            "mode": mode,
            "winner_idx": winner_idx,
            "eliminated": {"id": eliminated["id"], "name": eliminated["name"]},
            "winner": ({"id": actual_winner["id"], "name": actual_winner["name"]}
                       if actual_winner else None),
            "final_rotation": physics.final_rotation,
            "total_rotation": physics.total_rotation,
        })

    # Шлём WS spin_completed — все клиенты синхронизируют финальную позицию
    broadcast_winner = actual_winner if actual_winner else eliminated
    await ws_manager.broadcast_spin_completed(
        guild_id, spin_id,
        winner=anonymize_items([broadcast_winner])[0] if broadcast_winner else None,
        final_rotation=physics.final_rotation,
        mode=mode,
        eliminated=(anonymize_items([eliminated])[0]
                    if mode in ("elimination", "elimination_final") else None),
        remaining_count=max(0, len(items) - (2 if mode == "elimination_final" else 1)),
    )

    # Сначала обновляем список лотов у всех клиентов (winner удалён).
    # Это критично: без broadcast_wheel_updated фронт не уберёт победителя из
    # списка, и пользователю придётся перезагружать страницу.
    # Раньше тут была пауза 1с + долгий await bot.on_wheel_spin_completed —
    # он блокировал event loop (kinopoisk/telegram API могут висеть секунды),
    # и broadcast_wheel_updated уходил с задержкой или вообще не уходил.
    await asyncio.sleep(0.3)
    if is_test:
        remaining = await db.g_list_test_wheel_items(guild_id)
    else:
        remaining = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(guild_id, anonymize_items(remaining))

    # Чистим состояние активного спина
    _active_spins.pop(spin_id, None)
    # Освобождаем lock — с этого момента новая крутка возможна.
    # Дискорд/ТГ-постинг пойдёт в фоне и не должен блокировать UI.
    if lock is not None and lock.locked():
        try:
            lock.release()
        except RuntimeError:
            # Lock already released — race with another cleanup path
            pass

    # Постинг в Discord/TG — в фоновой задаче, чтобы не блокировать
    # завершение спина (event loop свободен для WS-бродкастов).
    # Логируем ошибки, но не роняем спин.
    if actual_winner:
        asyncio.create_task(_safe_post_to_external(actual_winner, is_test, spin_id))


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
        await ws_manager.broadcast_wheel_updated(guild_id, anonymize_items(items))

        return JSONResponse({
            "ok": True,
            "log_path": log_path,
            "cleanup_stats": cleanup_stats,
        })
    except Exception as e:
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
        await ws_manager.broadcast_wheel_updated(guild_id, anonymize_items(items))
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


# === Helpers для WS-recovery ===

def _find_active_spin_for_guild(guild_id: int) -> dict | None:
    """Найти активный (status == SPINNING) спин для данной гильдии.

    Возвращает dict {"spin_id": ..., "physics": ..., "mode": ..., "duration": ...,
                     "items": [...]} или None.
    Используется WS-recovery — опоздавший клиент получает spin_started и
    может запустить анимацию по параметрам physics.
    """
    for spin_id, state in _active_spins.items():
        if (state.get("guild_id") == guild_id
                and state.get("status") == "SPINNING"):
            return {
                "spin_id": spin_id,
                "physics": state["physics"],
                "mode": state["mode"],
                "duration": state["duration"],
                "items": state["items"],
            }
    return None


# === WebSocket: real-time обновления колеса ===

@router.websocket("/ws/wheel")
async def ws_wheel(websocket: WebSocket):
    """WebSocket для подписки на обновления колеса.

    guild_id передаётся как query-параметр: /ws/wheel?guild_id=123

    v2.4.0:
    - Подключение привязывается к guild_id (ws_manager.connect(ws, guild_id)).
    - При подключении сразу шлём текущее состояние (wheel_updated).
    - Recovery (P1 #6): если в этой гильдии есть активный спин (SPINNING),
      шлём spin_started с physics — опоздавший клиент подхватит анимацию.
    """
    import json

    # Читаем guild_id из query-параметров (по умолчанию 0)
    guild_id_str = websocket.query_params.get("guild_id", "0")
    try:
        guild_id = int(guild_id_str)
    except (ValueError, TypeError):
        guild_id = 0

    await ws_manager.connect(websocket, guild_id)
    try:
        # При первом подключении сразу шлём текущее состояние колеса (анонимизированное)
        items = await db.g_list_wheel_items(guild_id, active_only=True)
        safe_items = anonymize_items(items)
        await websocket.send_text(json.dumps({
            "type": "wheel_updated",
            "payload": {"items": safe_items, "count": len(safe_items)},
        }, ensure_ascii=False))

        # Recovery: если в этой гильдии есть активный спин (SPINNING) —
        # отправляем spin_started с physics, чтобы опоздавший клиент
        # подхватил анимацию и досмотрел до конца вместе со всеми.
        active_spin = _find_active_spin_for_guild(guild_id)
        if active_spin:
            physics: SpinPhysics = active_spin["physics"]
            await websocket.send_text(json.dumps({
                "type": "spin_started",
                "payload": {
                    "spin_id": active_spin["spin_id"],
                    "physics": physics_to_dict(physics),
                    "mode": active_spin["mode"],
                    "items": anonymize_items(active_spin["items"]),
                    "duration": active_spin["duration"],
                },
            }, ensure_ascii=False))

        # Держим соединение, ждём сообщений (клиент может слать ping)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        _log.debug("WS error: %s", e)
        await ws_manager.disconnect(websocket)
