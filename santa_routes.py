"""FastAPI APIRouter — маршруты модуля Тайный Санта.

Извлечено из web.py при рефакторинге (Task ID: refactor-ach-santa).
Содержит:
- HTML-страницу /santa (главная)
- HTML-страницу /santa/{event_id}/reveal (раскрытие для совместного просмотра)
- REST API /api/santa/* (create, join, set_ready, assign, mark_gift_sent,
  reveal, close)

Общие зависимости (auth, templates, anonymize_items) импортируются из deps.py —
без circular imports. is_santa_enabled() определена локально (асинхронная
версия, читает settings-кеш через db.get_setting) чтобы не тянуть зависимость
от web.py — это избегает circular import'а.
"""
from __future__ import annotations

from datetime import datetime as dt

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
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


# === Helpers ===

async def is_santa_enabled() -> bool:
    """Проверяет включён ли модуль Тайный Санта (тумблер в /features).

    Локальная версия (в web.py была is_santa_enabled_async / is_santa_enabled).
    Копия асинхронного варианта — читает `santa_enabled` setting из БД
    (db.get_setting сам использует in-memory кеш на 30 сек).
    """
    val = await db.get_setting("santa_enabled")
    return val == "1"


# === Santa: HTML-страницы ===

@router.get("/santa", response_class=HTMLResponse)
async def santa_main_page(request: Request, _user: dict = Depends(require_user)):
    """Главная страница модуля Тайный Санта.
    - Показывает активное событие (если есть)
    - Если событие в статусе collecting и юзер участвует — форма Steam-профиля
    - Если assigned — показывает «ваш получатель»
    - Если revealed — показывает общий список (только если участник)
    - Если нет события — для организатора кнопка «Создать событие»
    """
    if not await is_santa_enabled():
        raise HTTPException(status_code=404, detail="Santa module disabled")

    guild_id = get_current_guild_id(_user)
    # init_guild_tables уже вызывается при on_ready бота

    event = await db.g_get_active_santa_event(guild_id)
    user_discord_id = _user.get("discord_id", 0)
    my_participant = None
    my_assignment = None
    all_assignments = None
    participants = []
    ready_count = 0
    total_count = 0
    is_organizer = False

    if event:
        is_organizer = (event["created_by"] == user_discord_id) or _user.get("is_admin", False)
        participants = await db.g_list_santa_participants(guild_id, event["id"])
        total_count = len(participants)
        ready_count = sum(1 for p in participants if p["is_ready"])
        my_participant = await db.g_get_santa_participant(guild_id, event["id"], user_discord_id)
        if event["status"] == "assigned":
            my_assignment = await db.g_get_santa_assignment(guild_id, event["id"], user_discord_id)
        if event["status"] == "revealed":
            # После раскрытия все видят кто кому дарил
            all_assignments = await db.g_get_all_santa_assignments(guild_id, event["id"])

    # История событий
    all_events = await db.g_list_santa_events(guild_id)

    return templates.TemplateResponse(request, "santa.html", {
        "user": _user,
        "event": event,
        "my_participant": my_participant,
        "my_assignment": my_assignment,
        "all_assignments": all_assignments,
        "participants": participants,
        "ready_count": ready_count,
        "total_count": total_count,
        "is_organizer": is_organizer,
        "is_admin": _user.get("is_admin", False),
        "all_events": all_events,
        "min_participants": db.SANTA_MIN_PARTICIPANTS,
    })


@router.get("/santa/{event_id}/reveal", response_class=HTMLResponse)
async def santa_reveal_page(
    request: Request,
    event_id: int,
    _user: dict = Depends(require_user),
):
    """Страница раскрытия санты для стрима в Discord.
    Показывает все пары санта→получатель с анимацией появления.
    Доступна всем залогиненным (для совместного просмотра).
    """
    if not await is_santa_enabled():
        raise HTTPException(status_code=404, detail="Santa module disabled")
    guild_id = get_current_guild_id(_user)
    event = await db.g_get_santa_event(guild_id, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if event["status"] != "revealed":
        # Если ещё не раскрыт — редирект на главную санты
        return RedirectResponse(url="/santa", status_code=303)

    assignments = await db.g_get_all_santa_assignments(guild_id, event_id)
    return templates.TemplateResponse(request, "santa_reveal.html", {
        "user": _user,
        "event": event,
        "assignments": assignments,
    })


# === Santa: REST API ===

@router.post("/api/santa/create")
async def api_santa_create(
    _user: dict = Depends(require_user),
    title: str = Form("Тайный Санта"),
    deadline: str = Form(...),
    budget_note: str = Form(""),
):
    """Создать событие Тайного Санты. Любой юзер может создать (как сбор фильмов)."""
    if not await is_santa_enabled():
        return JSONResponse({"error": "Santa module disabled"}, status_code=404)
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    # Проверяем что нет активного события
    existing = await db.g_get_active_santa_event(guild_id)
    if existing:
        return JSONResponse({"error": "Уже есть активное событие санты. Завершите его сначала."}, status_code=400)

    # init_guild_tables уже вызывается при on_ready бота

    # Парсим дату (формат YYYY-MM-DD от <input type="date">)
    try:
        # Дедлайн = конец суток указанной даты (23:59:59 UTC)
        deadline_dt = dt.strptime(deadline, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        deadline_iso = deadline_dt.isoformat()
    except ValueError:
        return JSONResponse({"error": "Неверный формат даты. Используйте YYYY-MM-DD."}, status_code=400)

    event = await db.g_create_santa_event(
        guild_id, title.strip() or "Тайный Санта",
        deadline_iso, budget_note.strip() or None, user_discord_id,
    )
    return JSONResponse({"ok": True, "event": event})


@router.post("/api/santa/join")
async def api_santa_join(
    _user: dict = Depends(require_user),
    preferences: str = Form(""),
):
    """Присоединиться к событию санты.
    Steam-профиль берётся из профиля пользователя (должен быть указан заранее).
    Опционально принимает пожелания.
    """
    if not await is_santa_enabled():
        return JSONResponse({"error": "Santa module disabled"}, status_code=404)
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)
    if not user_discord_id:
        return JSONResponse({"error": "user not identified"}, status_code=400)

    event = await db.g_get_active_santa_event(guild_id)
    if not event:
        return JSONResponse({"error": "Нет активного события санты."}, status_code=400)
    if event["status"] != "collecting":
        return JSONResponse({"error": "Событие уже закрыто для регистрации."}, status_code=400)

    # Берём Steam-профиль из users (указан заранее в /profile)
    steam_profile = await db.get_user_steam_profile(user_discord_id)
    if not steam_profile or not steam_profile.get("steam_id64"):
        return JSONResponse({
            "error": "Сначала укажите ваш Steam-профиль на странице /profile.",
            "error_code": "no_steam_profile",
        }, status_code=400)

    # Добавляем/обновляем участника с Steam-данными из профиля
    await db.g_add_santa_participant(
        guild_id, event["id"], user_discord_id,
        username=_user.get("username"),
        display_name=_user.get("username"),
        avatar_url=_user.get("avatar_url"),
        steam_profile_url=steam_profile["steam_profile_url"],
        steam_id64=steam_profile["steam_id64"],
        steam_persona=steam_profile["steam_persona"],
        steam_avatar_url=steam_profile["steam_avatar_url"],
        preferences=preferences.strip() or None,
    )

    # v2.3: Проверка авто-ачивок после присоединения к Сайте
    try:
        await db.g_check_and_grant_auto(guild_id, user_discord_id, "santa_participations")
    except Exception:
        pass

    return JSONResponse({
        "ok": True,
        "steam_persona": steam_profile["steam_persona"],
        "steam_avatar_url": steam_profile["steam_avatar_url"],
    })


@router.post("/api/santa/set_ready")
async def api_santa_set_ready(
    _user: dict = Depends(require_user),
    is_ready: bool = Form(False),
):
    """Отметить себя готовым к распределению сант."""
    if not await is_santa_enabled():
        return JSONResponse({"error": "Santa module disabled"}, status_code=404)
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)

    event = await db.g_get_active_santa_event(guild_id)
    if not event or event["status"] != "collecting":
        return JSONResponse({"error": "Нет активного события в стадии сбора."}, status_code=400)

    # Проверяем что юзер — участник
    participant = await db.g_get_santa_participant(guild_id, event["id"], user_discord_id)
    if not participant:
        return JSONResponse({"error": "Вы не присоединились к событию."}, status_code=400)
    if not participant["steam_id64"]:
        return JSONResponse({"error": "Сначала укажите Steam-профиль."}, status_code=400)

    updated = await db.g_set_santa_participant_ready(guild_id, event["id"], user_discord_id, is_ready)
    return JSONResponse({"ok": updated, "is_ready": is_ready})


@router.post("/api/santa/assign")
async def api_santa_assign(_user: dict = Depends(require_user)):
    """Назначить сант (распределение). Только организатор или админ.
    Требует минимум 4 готовых участника.
    """
    if not await is_santa_enabled():
        return JSONResponse({"error": "Santa module disabled"}, status_code=404)
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)

    event = await db.g_get_active_santa_event(guild_id)
    if not event or event["status"] != "collecting":
        return JSONResponse({"error": "Нет активного события в стадии сбора."}, status_code=400)

    is_organizer = (event["created_by"] == user_discord_id) or _user.get("is_admin", False)
    if not is_organizer:
        return JSONResponse({"error": "Только организатор или админ может запустить распределение."}, status_code=403)

    ready_count = await db.g_count_santa_participants(guild_id, event["id"], ready_only=True)
    if ready_count < db.SANTA_MIN_PARTICIPANTS:
        return JSONResponse({
            "error": f"Нужно минимум {db.SANTA_MIN_PARTICIPANTS} готовых участников. Сейчас: {ready_count}.",
        }, status_code=400)

    assignments = await db.g_assign_santas(guild_id, event["id"])
    if not assignments:
        return JSONResponse({"error": "Не удалось распределить. Возможно слишком мало участников."}, status_code=400)

    # Отправляем TG-уведомления каждому сантy
    try:
        import bot as bot_module
        import telegram
        bot_instance = bot_module.get_bot_instance()
        if bot_instance:
            for a in assignments:
                santa_id = a["santa_discord_id"]
                recipient_id = a["recipient_discord_id"]
                # Достаём инфо о получателе
                recipient = await db.g_get_santa_participant(guild_id, event["id"], recipient_id)
                if not recipient:
                    continue
                # Проверяем что санта привязал TG
                if not await db.is_tg_linked(santa_id):
                    continue
                # Проверяем тумблер — для санты используем notify_collection_started (переиспользуем)
                # TODO: добавить отдельный тумблер tg_notify_santa в будущей версии
                # Пока шлём всем привязанным сантам
                tg_link = await db.get_tg_link(santa_id)
                if not tg_link or not tg_link[1]:  # tg_user_id
                    continue
                tg_user_id = str(tg_link[1])
                recipient_name = recipient["display_name"] or recipient["username"] or f"User#{recipient_id}"
                text = (
                    f"🎅 <b>Тайный Санта назначил вам получателя!</b>\n\n"
                    f"Вы дарите подарок: <b>{__import__('html').escape(recipient_name)}</b>\n\n"
                    f"🎮 Steam-профиль получателя:\n{__import__('html').escape(recipient.get('steam_profile_url') or 'не указан')}\n\n"
                )
                if recipient.get("preferences"):
                    text += f"💬 Пожелания получателя:\n<i>{__import__('html').escape(recipient['preferences'])}</i>\n\n"
                text += "Зайдите на /santa чтобы посмотреть вишлист получателя и отметить подарок отправленным."
                # Получаем TG-токен
                raw_token = await db.get_setting("telegram_token")
                if raw_token:
                    tg_token = crypto.decrypt(raw_token)
                    await telegram.send_message(tg_token, tg_user_id, text)
    except Exception as e:
        import logging
        logging.getLogger("web").warning("Santa TG notify failed: %s", e)

    return JSONResponse({"ok": True, "assignments_count": len(assignments)})


@router.post("/api/santa/mark_gift_sent")
async def api_santa_mark_gift_sent(
    _user: dict = Depends(require_user),
    gift_note: str = Form(""),
):
    """Отметить что подарок отправлен получателю.
    Триггерит TG-уведомление получателю «Вам отправили подарок!».
    """
    if not await is_santa_enabled():
        return JSONResponse({"error": "Santa module disabled"}, status_code=404)
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)

    event = await db.g_get_active_santa_event(guild_id)
    if not event or event["status"] != "assigned":
        return JSONResponse({"error": "Нет активного события в стадии назначения."}, status_code=400)

    # Получаем назначение санты
    assignment = await db.g_get_santa_assignment(guild_id, event["id"], user_discord_id)
    if not assignment:
        return JSONResponse({"error": "Вы не назначены как санта."}, status_code=400)
    if assignment["gift_sent_at"]:
        return JSONResponse({"error": "Подарок уже отмечен как отправленный."}, status_code=400)

    # Отмечаем
    marked = await db.g_mark_santa_gift_sent(
        guild_id, event["id"], user_discord_id,
        gift_note.strip() or None,
    )
    if not marked:
        return JSONResponse({"error": "Не удалось отметить."}, status_code=500)

    # Отправляем TG-уведомление получателю
    recipient_id = assignment["recipient_discord_id"]
    try:
        if await db.is_tg_linked(recipient_id):
            tg_link = await db.get_tg_link(recipient_id)
            if tg_link and tg_link[1]:
                tg_user_id = str(tg_link[1])
                raw_token = await db.get_setting("telegram_token")
                if raw_token:
                    tg_token = crypto.decrypt(raw_token)
                    santa_name = _user.get("username", "Тайный Санта")
                    import html as html_module
                    text = (
                        f"🎁 <b>Ваш Тайный Санта отправил вам подарок!</b>\n\n"
                        f"Скоро он прибудет в ваш Steam. Приятной игры! 🎮"
                    )
                    if gift_note.strip():
                        text += f"\n\n💬 Послание от санты:\n<i>{html_module.escape(gift_note.strip())}</i>"
                    import telegram
                    await telegram.send_message(tg_token, tg_user_id, text)
    except Exception as e:
        import logging
        logging.getLogger("web").warning("Santa gift_sent TG notify failed: %s", e)

    return JSONResponse({"ok": True})


@router.post("/api/santa/reveal")
async def api_santa_reveal(
    _user: dict = Depends(require_admin),
    event_id: int = Form(...),
):
    """Раскрыть сант (только админ). Статус → revealed."""
    if not await is_santa_enabled():
        return JSONResponse({"error": "Santa module disabled"}, status_code=404)
    guild_id = get_current_guild_id(_user)
    user_discord_id = _user.get("discord_id", 0)

    revealed = await db.g_reveal_santa_event(guild_id, event_id, user_discord_id)
    if not revealed:
        return JSONResponse({"error": "Не удалось раскрыть. Возможно событие не в стадии assigned."}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/api/santa/close")
async def api_santa_close(
    _user: dict = Depends(require_admin),
    event_id: int = Form(...),
):
    """Закрыть событие санты (только админ). Скрывает все данные."""
    if not await is_santa_enabled():
        return JSONResponse({"error": "Santa module disabled"}, status_code=404)
    guild_id = get_current_guild_id(_user)
    closed = await db.g_close_santa_event(guild_id, event_id)
    if not closed:
        return JSONResponse({"error": "Событие не найдено."}, status_code=404)
    return JSONResponse({"ok": True})
