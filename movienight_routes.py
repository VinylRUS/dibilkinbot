"""FastAPI APIRouter — маршруты киновечера (movienight) и сборов фильмов (collection).

Извлечено из web.py при рефакторинге (Task ID: refactor-routes).
Содержит:
- HTML-страницу /movienight
- Редирект по токену сбора /collect/{token}
- REST API /api/collection/* (start, save_picks, set_ready, kick, cancel, start_spin, status)

Общие зависимости (auth, templates, anonymize_items) импортируются из deps.py —
без circular imports.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import crypto
import db
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


# === Movienight: HTML-страница ===

@router.get("/movienight", response_class=HTMLResponse)
async def movienight_page(request: Request, _user: dict = Depends(require_user)):
    """Главная страница киновечера.
    - Если нет активного сбора → «Сбор пока закрыт» + форма «Начать сбор» (для любого юзера)
    - Если есть активный сбор:
      - Обычный юзер видит свой вишлист с чекбоксами (выбор до N фильмов) + «Готов»
      - Организатор/админ видит также панель участников (кто готов, кто нет)
    """
    guild_id = get_current_guild_id(_user)
    # init_guild_tables уже вызывается при on_ready бота

    collection = await db.g_get_active_collection(guild_id)
    user_discord_id = _user.get("discord_id", 0)
    user_picks: list[dict] = []
    watchlist: list[tuple] = []
    participants: list[dict] = []
    is_organizer = False
    user_picks_count = 0

    if collection:
        # Добавляем текущего юзера в участники (если ещё нет)
        is_new_participant = await db.g_add_collection_participant(
            guild_id, collection["id"], user_discord_id,
            username=_user.get("username"),
            display_name=_user.get("username"),
        )
        # Достаём список участников
        participants = await db.g_list_collection_participants(guild_id, collection["id"])
        # Достаём выбор текущего юзера
        user_picks = await db.g_get_user_picks(guild_id, collection["id"], user_discord_id)
        user_picks_count = len(user_picks)
        # Достаём вишлист юзера (только непросмотренные)
        watchlist = await db.g_list_watchlist(guild_id, user_discord_id, include_watched=False)
        # Проверка — организатор ли текущий юзер
        is_organizer = (collection["started_by"] == user_discord_id) or _user.get("is_admin", False)

    # Проверка bot_instance для Discord-анонсов
    return templates.TemplateResponse(request, "movienight.html", {
        "user": _user,
        "collection": collection,
        "watchlist": watchlist,
        "user_picks": user_picks,
        "user_picks_count": user_picks_count,
        "max_per_user": collection["max_per_user"] if collection else 5,
        "participants": participants,
        "is_organizer": is_organizer,
        "is_admin": _user.get("is_admin", False),
    })


@router.get("/collect/{token}", response_class=HTMLResponse)
async def collect_by_token(request: Request, token: str, _user: dict = Depends(require_user)):
    """Прямой заход по ссылке из Discord-анонса. Просто редирект на /movienight.
    Токен нужен только для проверки что сбор существует.
    """
    guild_id = get_current_guild_id(_user)
    collection = await db.g_get_collection_by_token(guild_id, token)
    if not collection:
        return RedirectResponse(url="/movienight?error=invalid_token", status_code=303)
    if collection["status"] != "active":
        return RedirectResponse(url="/movienight?error=not_active", status_code=303)
    # Редирект на /movienight — там юзер автоматически добавится в участники
    return RedirectResponse(url="/movienight?joined=1", status_code=303)


# === Collection API ===

@router.post("/api/collection/start")
async def api_collection_start(
    _user: dict = Depends(require_user),
    max_per_user: int = Form(5),
):
    """Запустить новый сбор. Любой юзер может начать.
    Лимит: 1-10 фильмов на участника.
    Также постит анонс в Discord-канал #анонсов (если настроен).
    """
    if max_per_user < 1:
        max_per_user = 1
    if max_per_user > 10:
        max_per_user = 10

    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    # init_guild_tables уже вызывается при on_ready бота

    collection = await db.g_start_collection(guild_id, user_discord_id, max_per_user)

    # Постим анонс в Discord-канал
    panel_url = await db.get_setting("panel_base_url")
    announce_channel_id_str = await db.get_setting("channel_announce_id")
    if announce_channel_id_str and announce_channel_id_str.isdigit() and panel_url:
        try:
            import bot as bot_module
            import discord
            bot_instance = bot_module.get_bot_instance()
            if bot_instance:
                channel = bot_instance.get_channel(int(announce_channel_id_str))
                if channel:
                    collect_link = f"{panel_url.rstrip('/')}/collect/{collection['token']}"
                    embed = discord.Embed(
                        title="🎬 Сбор фильмов начат!",
                        description=(
                            f"Лимит: **{max_per_user}** фильм(ов) на участника\n\n"
                            f"📍 Перейдите по ссылке чтобы выбрать фильмы:\n{collect_link}\n\n"
                            f"Выберите ровно **{max_per_user}** фильмов из вашего списка желаемого и нажмите «Готов»."
                        ),
                        color=0xFFB703,
                        timestamp=datetime.utcnow(),
                    )
                    embed.set_footer(text=f"Запустил: {_user.get('username', 'unknown')}")
                    await channel.send(embed=embed)
        except Exception as e:
            import logging
            logging.getLogger("web").warning("Discord collection announce failed: %s", e)

    # Добавляем организатора в участники
    await db.g_add_collection_participant(
        guild_id, collection["id"], user_discord_id,
        username=_user.get("username"),
        display_name=_user.get("username"),
    )

    # WebSocket событие
    await ws_manager.broadcast_collection_started(guild_id, collection)

    # Персональное TG-уведомление организатору «Ты начал сбор фильмов, жди участников»
    # Если организатор не привязал TG — пингнем его в Discord ephemeral
    try:
        import bot as bot_module
        bot_instance = bot_module.get_bot_instance()
        if bot_instance:
            # Проверяем привязку
            is_linked = await db.is_tg_linked(user_discord_id)
            if is_linked:
                # Проверяем что у организатора включён тумблер — если да, шлём
                settings_dict = await db.get_user_tg_settings(user_discord_id)
                if settings_dict.get("tg_notify_collection_started"):
                    await bot_instance.notify_collection_started(user_discord_id, max_per_user)
                # Иначе не беспокоим — юзер выключил уведомления осознанно
            else:
                # Не привязан — пингнем в Discord
                await bot_instance.ping_unlinked_organizer(user_discord_id)
    except Exception as e:
        import logging
        logging.getLogger("web").warning("TG notify collection_started failed: %s", e)

    # v2.3: Проверка авто-ачивок после создания сбора
    try:
        await db.g_check_and_grant_auto(guild_id, user_discord_id, "collections_started")
    except Exception:
        pass

    return JSONResponse({"ok": True, "collection": collection})


@router.post("/api/collection/save_picks")
async def api_collection_save_picks(
    _user: dict = Depends(require_user),
    watchlist_ids: str = Form(""),  # comma-separated list of watchlist IDs
):
    """Сохранить выбор юзера в сборе. Замещает предыдущий выбор.
    watchlist_ids — строка с ID через запятую: "1,5,12"
    """
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    collection = await db.g_get_active_collection(guild_id)
    if not collection:
        return JSONResponse({"error": "no active collection"}, status_code=400)

    # Парсим watchlist_ids
    try:
        ids = [int(x.strip()) for x in watchlist_ids.split(",") if x.strip().isdigit()]
    except Exception:
        ids = []

    # Проверяем что ровно max_per_user
    max_per_user = collection["max_per_user"]
    if len(ids) != max_per_user:
        return JSONResponse({
            "error": f"Нужно выбрать ровно {max_per_user} фильм(ов). Выбрано: {len(ids)}.",
        }, status_code=400)

    # Достаём вишлист юзера чтобы валидировать что эти ID его
    watchlist = await db.g_list_watchlist(guild_id, user_discord_id, include_watched=False)
    watchlist_map = {w[0]: w for w in watchlist}  # id → tuple

    picks = []
    for wid in ids:
        if wid not in watchlist_map:
            return JSONResponse({"error": f"Фильм #{wid} не найден в вашем списке желаемого"}, status_code=400)
        w_id, title, tmdb_id, _, _ = watchlist_map[wid]
        picks.append({"watchlist_id": w_id, "title": title, "tmdb_id": tmdb_id})

    # Сохраняем
    count = await db.g_save_collection_picks(guild_id, collection["id"], user_discord_id, picks)
    # Авто-отмечаем «Готов»
    await db.g_set_participant_ready(guild_id, collection["id"], user_discord_id, True)
    # WebSocket (без раскрытия какие именно фильмы)
    await ws_manager.broadcast_collection_picks_updated(guild_id, user_discord_id, count)

    # Проверяем все ли теперь готовы — если да, уведомляем организатора
    all_ready = False
    ready_count = 0
    participants = await db.g_list_collection_participants(guild_id, collection["id"])
    active_participants = [p for p in participants if not p["kicked_at"]]
    ready_count = sum(1 for p in active_participants if p["is_ready"])
    if active_participants and ready_count == len(active_participants):
        all_ready = True
        try:
            import asyncio
            import bot as bot_module
            bot_instance = bot_module.get_bot_instance()
            if bot_instance:
                asyncio.create_task(
                    bot_instance.notify_collection_all_ready(
                        collection["started_by"], ready_count
                    )
                )
        except Exception as e:
            import logging
            logging.getLogger("web").warning("notify_collection_all_ready failed: %s", e)

    return JSONResponse({"ok": True, "picks_count": count, "is_ready": True, "all_ready": all_ready, "ready_count": ready_count})


@router.post("/api/collection/set_ready")
async def api_collection_set_ready(
    _user: dict = Depends(require_user),
    is_ready: bool = Form(False),
):
    """Отметить себя готовым/не готовым."""
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    collection = await db.g_get_active_collection(guild_id)
    if not collection:
        return JSONResponse({"error": "no active collection"}, status_code=400)

    # Если юзер хочет стать готовым — проверяем что у него есть ровно max_per_user picks
    if is_ready:
        count = await db.g_count_user_picks(guild_id, collection["id"], user_discord_id)
        if count != collection["max_per_user"]:
            return JSONResponse({
                "error": f"Чтобы стать готовым, нужно выбрать ровно {collection['max_per_user']} фильм(ов). Сейчас: {count}.",
            }, status_code=400)

    updated = await db.g_set_participant_ready(guild_id, collection["id"], user_discord_id, is_ready)
    # WebSocket
    participant = {
        "user_discord_id": user_discord_id,
        "is_ready": is_ready,
    }
    await ws_manager.broadcast_collection_participant_ready(guild_id, participant)

    # Если юзер стал готовым — проверяем все ли теперь готовы
    # Если да — отправляем уведомление «Все готовы, можно начинать крутить!» организатору
    all_ready = False
    ready_count = 0
    if is_ready and updated:
        participants = await db.g_list_collection_participants(guild_id, collection["id"])
        active_participants = [p for p in participants if not p["kicked_at"]]
        ready_count = sum(1 for p in active_participants if p["is_ready"])
        if active_participants and ready_count == len(active_participants):
            all_ready = True
            # Отправляем уведомление организатору (асинхронно, не блокируя ответ)
            try:
                import asyncio
                import bot as bot_module
                bot_instance = bot_module.get_bot_instance()
                if bot_instance:
                    asyncio.create_task(
                        bot_instance.notify_collection_all_ready(
                            collection["started_by"], ready_count
                        )
                    )
            except Exception as e:
                import logging
                logging.getLogger("web").warning("notify_collection_all_ready failed: %s", e)

    return JSONResponse({
        "ok": updated,
        "is_ready": is_ready,
        "all_ready": all_ready,
        "ready_count": ready_count,
    })


@router.post("/api/collection/kick")
async def api_collection_kick(
    _user: dict = Depends(require_user),
    target_user_id: int = Form(...),
):
    """Кикнуть участника. Только организатор или админ."""
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    collection = await db.g_get_active_collection(guild_id)
    if not collection:
        return JSONResponse({"error": "no active collection"}, status_code=400)

    is_organizer = (collection["started_by"] == user_discord_id) or _user.get("is_admin", False)
    if not is_organizer:
        return JSONResponse({"error": "only organizer or admin can kick"}, status_code=403)

    if target_user_id == collection["started_by"]:
        return JSONResponse({"error": "cannot kick organizer"}, status_code=400)

    kicked = await db.g_kick_collection_participant(guild_id, collection["id"], target_user_id, user_discord_id)
    if not kicked:
        return JSONResponse({"error": "participant not found or already kicked"}, status_code=404)

    await ws_manager.broadcast_collection_participant_kicked(guild_id, target_user_id, user_discord_id)
    return JSONResponse({"ok": True, "kicked_user_id": target_user_id})


@router.post("/api/collection/cancel")
async def api_collection_cancel(_user: dict = Depends(require_user)):
    """Отменить сбор. Только организатор или админ."""
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    collection = await db.g_get_active_collection(guild_id)
    if not collection:
        return JSONResponse({"error": "no active collection"}, status_code=400)

    is_organizer = (collection["started_by"] == user_discord_id) or _user.get("is_admin", False)
    if not is_organizer:
        return JSONResponse({"error": "only organizer or admin can cancel"}, status_code=403)

    cancelled = await db.g_cancel_collection(guild_id, collection["id"], user_discord_id)
    await ws_manager.broadcast_collection_cancelled(guild_id, user_discord_id)
    return JSONResponse({"ok": cancelled})


@router.post("/api/collection/start_spin")
async def api_collection_start_spin(
    _user: dict = Depends(require_user),
    force: bool = Form(False),
):
    """Начать крутку колеса. Только организатор или админ.
    Если force=False — требует что ВСЕ участники (не кикнутые) были готовы.
    Если force=True — запускает даже с неготовыми.
    Берёт все picks, перемешивает, добавляет в колесо, завершает сбор.
    """
    import random
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    collection = await db.g_get_active_collection(guild_id)
    if not collection:
        return JSONResponse({"error": "no active collection"}, status_code=400)

    is_organizer = (collection["started_by"] == user_discord_id) or _user.get("is_admin", False)
    if not is_organizer:
        return JSONResponse({"error": "only organizer or admin can start spin"}, status_code=403)

    # Проверяем что все готовы (если не force)
    if not force:
        participants = await db.g_list_collection_participants(guild_id, collection["id"])
        active_participants = [p for p in participants if not p["kicked_at"]]
        not_ready = [p for p in active_participants if not p["is_ready"]]
        if not_ready:
            return JSONResponse({
                "error": f"Не все готовы: {len(not_ready)} из {len(active_participants)} участников не отметились. Используйте «Начать принудительно» если хотите запустить без них.",
                "not_ready_count": len(not_ready),
                "total_count": len(active_participants),
            }, status_code=400)

    # Завершаем сбор — достаём и шафлим picks
    picks = await db.g_complete_collection_spin(guild_id, collection["id"], user_discord_id)
    if not picks:
        # Пустой список может означать:
        # 1. Сбор уже завершён другим одновременным вызовом (race condition защита)
        # 2. В сборе действительно не было picks (никто ничего не добавил)
        # Проверяем текущий статус — если уже 'completed', возвращаем OK с redirect
        # на /wheel (пользователь увидит уже загруженное колесо).
        collection_after = await db.g_get_active_collection(guild_id)
        if not collection_after:
            # Сбор завершён — колесо уже должно быть заполнено предыдущим вызовом
            return JSONResponse({
                "ok": True,
                "picks_count": 0,
                "already_completed": True,
                "redirect": "/wheel",
            })
        return JSONResponse({"error": "no picks found"}, status_code=400)

    # Очищаем колесо перед загрузкой
    await db.g_clear_wheel(guild_id)

    # Загружаем picks в колесо (уже перемешаны)
    for pick in picks:
        await db.g_add_wheel_item(guild_id, pick["title"], pick.get("tmdb_id"), pick["user_discord_id"])

    # WebSocket: коллекция завершена + обновлённое колесо
    await ws_manager.broadcast_collection_completed(guild_id, len(picks))
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    await ws_manager.broadcast_wheel_updated(guild_id, anonymize_items(items))

    return JSONResponse({
        "ok": True,
        "picks_count": len(picks),
        "wheel_count": len(items),
        "redirect": "/wheel",
    })


@router.get("/api/collection/status")
async def api_collection_status(_user: dict = Depends(require_user)):
    """Получить статус активного сбора (для real-time обновлений через polling)."""
    guild_id = get_current_guild_id(_user)
    collection = await db.g_get_active_collection(guild_id)
    if not collection:
        return JSONResponse({"active": False})

    participants = await db.g_list_collection_participants(guild_id, collection["id"])
    user_discord_id = _user.get("discord_id", 0)
    user_picks_count = await db.g_count_user_picks(guild_id, collection["id"], user_discord_id)

    return JSONResponse({
        "active": True,
        "collection": collection,
        "participants": [
            {
                "user_discord_id": p["user_discord_id"],
                "display_name": p["display_name"] or p["username"],
                "avatar_url": p["avatar_url"],
                "is_ready": p["is_ready"],
                "kicked_at": p["kicked_at"],
                "picks_count": await db.g_count_user_picks(guild_id, collection["id"], p["user_discord_id"]) if not p["kicked_at"] else 0,
            }
            for p in participants
        ],
        "user_picks_count": user_picks_count,
    })
