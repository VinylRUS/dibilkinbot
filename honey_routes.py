"""FastAPI APIRouter — маршруты «Мёда» 🍯 v3.15.

v3.15.1: страница /honey перестроена — всё интерактивное разъехалось по
своим местам (решение босса «плашки разъехались»):
- Уровень пасечника → бейдж на публичном профиле (/u/{id})
- Подарочный мёд → панелька на чужом профиле рядом с кнопкой подписки
- Лотерея → дашборд, под карточкой мёда
- Ставки на колесо → страница /wheel, приём до первой крутки
- Поймай пчелу → нигде не упоминается (сюрприз, случайный канал)
- Магазин рамок → настройки профиля (/profile)
- Гильдии + общий банк → админ-панель (только Матка)
/honey — компактный «кошелёк»: баланс, как копится мёд, последние операции.

REST API (состав не менялся):
  - POST /api/honey/gift          (#31: P2P-перевод с комиссией 10% → банк)
  - GET  /api/honey/level         (#52: уровень и прогресс-бар)
  - POST /api/honey/frame/{key}   (#27: статичные рамки, 100 мёда)
  - POST /api/honey/gifframe/{key}(#45: GIF-рамки, 200 мёда — финальный этап)
  - GET  /api/honey/bank + POST /api/honey/bank/adjust (только Матка)
  - GET  /api/honey/lottery + POST /api/honey/lottery/buy (#14)
  - GET  /api/honey/bets + POST /api/honey/bets/place (#15; окно — до первой крутки)
  - GET/POST/DELETE /api/honey/clans (+members) — только Матка (#51)

Стиль — как cl_routes.py (deps импорты, JSONResponse с snowflake → str).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

import db
import honey_economy as he
from deps import (
    JSONResponse,
    get_current_guild_id,
    require_superuser,
    require_user,
    templates,
)

router = APIRouter()

_log = logging.getLogger("honey_routes")


# ============================================================
# Страница /honey
# ============================================================

@router.get("/honey", response_class=HTMLResponse)
async def honey_page(request: Request, _user: dict = Depends(require_user)):
    """Кошелёк «Мёд» v3.15.1: баланс, разбивка «как копится», последние операции.

    Интерактивные блоки v3.15 разъехались по своим местам: уровень — бейдж на
    публичном профиле; подарки — панелька на чужом профиле; лотерея — дашборд;
    ставки — страница колеса; рамки — настройки профиля; гильдии и банк —
    админ-панель. Поймай пчелу — сюрприз, на страницах не упоминается.
    """
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)

    balance = await db.g_honey_balance(guild_id, uid) if uid else 0
    level = await he.he_level(guild_id, uid) if uid else None
    breakdown = await db.g_honey_breakdown(guild_id, uid) if uid else None
    ledger = await db.g_honey_ledger_recent(guild_id, uid, limit=12) if uid else []

    return templates.TemplateResponse(request, "honey.html", {
        "user": _user,
        "balance": balance,
        "level": level,
        "breakdown": breakdown,
        "ledger": ledger,
        "honey_per": {
            "watched": db.HONEY_PER_WATCHED,
            "rating": db.HONEY_PER_RATING,
            "quote": db.HONEY_PER_QUOTE,
            "achievement": db.HONEY_PER_ACHIEVEMENT,
        },
        "gift_commission": he.HONEY_GIFT_COMMISSION,
        "active": "honey",
    })


# ============================================================
# #31: Подарочный мёд (P2P с комиссией 10%)
# ============================================================

@router.post("/api/honey/gift")
async def api_honey_gift(_user: dict = Depends(require_user),
                         to_discord_id: int = Form(...),
                         amount: int = Form(...)):
    """Перевести мёд другому участнику. Комиссия 10% → общий банк сервера.

    Комиссия сама душит перекидку ради фарма — доп. лимитов нет (скоуп v3.15).
    """
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    if not uid:
        return JSONResponse({"error": "no user"}, status_code=400)
    amount = int(amount)
    if amount < 1 or amount > 10_000:
        return JSONResponse({"error": "amount must be 1..10000"}, status_code=400)

    result = await he.he_gift(guild_id, uid, int(to_discord_id), amount)
    if not result.get("ok"):
        return JSONResponse({"error": result.get("error"),
                             "balance": result.get("balance")}, status_code=400)
    try:
        await db.g_log_activity(guild_id, "honey_gift", uid,
                                actor_name=_user.get("username") or _user.get("display_name"),
                                target_name=str(to_discord_id))
    except Exception:
        pass
    return JSONResponse({"ok": True, "delivered": result["delivered"],
                         "commission": result["commission"],
                         "balance": result["balance"]})


# ============================================================
# #52: Уровень пасечника
# ============================================================

@router.get("/api/honey/level")
async def api_honey_level(_user: dict = Depends(require_user)):
    """Уровень (каждые 100 суммарно заработанных) + прогресс до следующего."""
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    if not uid:
        return JSONResponse({"error": "no user"}, status_code=400)
    level = await he.he_level(guild_id, uid)
    return JSONResponse({"ok": True, **level,
                         "balance": await db.g_honey_balance(guild_id, uid)})


# ============================================================
# #27/#45: Рамки аватара (статичные / GIF)
# ============================================================

@router.post("/api/honey/frame/{frame_key}")
async def api_honey_buy_frame(frame_key: str, _user: dict = Depends(require_user)):
    """Купить статичную рамку (100 мёда) через generic g_cosmetic_buy."""
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    if not uid:
        return JSONResponse({"error": "no user"}, status_code=400)
    result = await he.he_frame_buy(guild_id, uid, frame_key, gif=False)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.post("/api/honey/gifframe/{frame_key}")
async def api_honey_buy_gif_frame(frame_key: str, _user: dict = Depends(require_user)):
    """Купить GIF-рамку (200 мёда). Механика готова; графика — финальный этап."""
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    if not uid:
        return JSONResponse({"error": "no user"}, status_code=400)
    result = await he.he_frame_buy(guild_id, uid, frame_key, gif=True)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.post("/api/honey/frame_equip/{frame_key}")
async def api_honey_frame_equip(frame_key: str, _user: dict = Depends(require_user)):
    """v3.16.0: надеть/снять рамку. 'none' — снять текущую.

    Рамка — СЕРВЕРНОЕ состояние (honey_user_frame): видна всем на
    публичном профиле, в отличие от accent-цвета на куках. Эквип не
    стоит мёда — платится только покупка.
    """
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    if not uid:
        return JSONResponse({"error": "no user"}, status_code=400)
    key = None if frame_key.lower() == "none" else frame_key
    result = await he.he_frame_equip(guild_id, uid, key)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.post("/api/honey/bee/launch")
async def api_honey_bee_launch(_user: dict = Depends(require_superuser)):
    """v3.16.0: ручной запуск «Поймай пчелу» (только Матка, из админки).

    Пчела летит в случайный ПУБЛИЧНЫЙ канал — как автоматический раунд.
    Открытый раунд блокирует запуск (двойные призы); кулдаун обходим
    сознательно («запустить сейчас»). Требует живой процесс бота.
    """
    guild_id = get_current_guild_id(_user)
    actor = _user.get("discord_id", 0)
    try:
        import bot as bot_module
        bot_instance = bot_module.get_bot_instance()
        if not bot_instance or not bot_instance.is_ready():
            return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)
        cog = bot_instance.get_cog("HoneyCog")
        if cog is None:
            return JSONResponse({"ok": False, "error": "honey cog not loaded"}, status_code=503)
        result = await cog.manual_bee_launch(int(guild_id))
    except Exception as e:
        _log.warning("manual bee launch failed: %s", e, exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if not result.get("ok"):
        code = 409 if result.get("error") == "bee_already_flying" else 400
        return JSONResponse(result, status_code=code)
    try:
        await db.g_log_activity(guild_id, "honey_bee_manual", actor,
                                actor_name=_user.get("username") or _user.get("display_name"),
                                target_name=str(result.get("channel_id", "")))
    except Exception:
        pass
    return JSONResponse(result)


# ============================================================
# Общий банк сервера — только Матка (superuser)
# ============================================================

@router.get("/api/honey/bank")
async def api_honey_bank(_user: dict = Depends(require_superuser)):
    """Баланс банка + история операций (только Матка)."""
    guild_id = get_current_guild_id(_user)
    return JSONResponse({
        "ok": True,
        "balance": await he.he_bank_balance(guild_id),
        "log": await he.he_bank_log(guild_id, limit=50),
    })


@router.post("/api/honey/bank/adjust")
async def api_honey_bank_adjust(_user: dict = Depends(require_superuser),
                                delta: int = Form(...),
                                reason: str = Form("")):
    """Ручное начисление/списание из банка (например, выдать приз вручную).

    Расходовать банк может только Матка — решение босса (скоуп v3.15).
    """
    guild_id = get_current_guild_id(_user)
    actor = _user.get("discord_id", 0)
    delta = int(delta)
    reason = (reason or "").strip()[:128] or "manual adjust"
    if delta == 0:
        return JSONResponse({"error": "delta must be non-zero"}, status_code=400)
    if delta > 0:
        await he.he_bank_deposit(guild_id, delta, reason, actor)
    else:
        if not await he.he_bank_withdraw(guild_id, -delta, reason, actor):
            return JSONResponse({"error": "insufficient bank"}, status_code=400)
    try:
        await db.g_log_activity(guild_id, "honey_bank_adjust", actor,
                                actor_name=_user.get("username") or _user.get("display_name"),
                                target_name=reason)
    except Exception:
        pass
    return JSONResponse({"ok": True, "delta": delta,
                         "balance": await he.he_bank_balance(guild_id)})


# ============================================================
# #14: Лотерея «Улей»
# ============================================================

@router.get("/api/honey/lottery")
async def api_honey_lottery(_user: dict = Depends(require_user)):
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    info = await he.he_lottery_info(guild_id, uid or None)
    return JSONResponse({"ok": True, **info})


@router.post("/api/honey/lottery/buy")
async def api_honey_lottery_buy(_user: dict = Depends(require_user),
                                qty: int = Form(1)):
    """Купить билеты (10 мёда за штуку). Розыгрыш — воскресенье 20:00 UTC."""
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    if not uid:
        return JSONResponse({"error": "no user"}, status_code=400)
    result = await he.he_lottery_buy(guild_id, uid, int(qty))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


# ============================================================
# #15: Ставки на сектор колеса
# ============================================================

@router.get("/api/honey/bets")
async def api_honey_bets(_user: dict = Depends(require_user)):
    """Открытые ставки + пул + сектора (активные лоты колеса)."""
    guild_id = get_current_guild_id(_user)
    open_bets = await he.he_bets_open(guild_id)
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    return JSONResponse({
        "ok": True,
        "bets": open_bets["bets"],
        "pool": open_bets["pool"],
        "by_item": open_bets["by_item"],
        "items": [{"id": i["id"], "name": i["name"], "color": i["color"]}
                  for i in items],
    })


@router.post("/api/honey/bets/place")
async def api_honey_bet_place(_user: dict = Depends(require_user),
                              item_name: str = Form(...),
                              amount: int = Form(...)):
    """Поставить мёд на сектор. v3.15.1: окно приёма — только ДО начала
    первой крутки текущей загрузки колеса (после — ставки невозможны);
    расчёт — автоматически при определении победителя крутки."""
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    if not uid:
        return JSONResponse({"error": "no user"}, status_code=400)

    # Сектор должен быть в активных лотах колеса
    items = await db.g_list_wheel_items(guild_id, active_only=True)
    if not any(i["name"] == item_name for i in items):
        return JSONResponse({"error": "unknown sector",
                             "error_code": "unknown_sector"}, status_code=400)

    # v3.15.1: приём ставок — до начала первой крутки раунда. Спин идёт прямо
    # сейчас или победитель этой загрузки уже определён → окно закрыто.
    round_open, closed_reason = await he.he_bets_round_open(guild_id)
    if not round_open:
        if closed_reason == "колесо крутится":
            return JSONResponse({"error": "Колесо крутится — ставки закрыты до результата.",
                                 "error_code": "spin_in_progress"}, status_code=409)
        return JSONResponse({
            "error": "Приём ставок закрыт: крутка этого колеса уже началась — "
                     "дождитесь нового сбора.",
            "error_code": "bets_closed"}, status_code=409)

    result = await he.he_bet_place(guild_id, uid, item_name, int(amount))
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


# ============================================================
# Гильдии (кланы) — создаёт и управляет только Матка
# ============================================================

@router.get("/api/honey/clans")
async def api_honey_clans(_user: dict = Depends(require_user)):
    """Список гильдий (читать могут все, управляет — только Матка)."""
    guild_id = get_current_guild_id(_user)
    return JSONResponse({"ok": True, "clans": await he.he_clan_list(guild_id)})


@router.post("/api/honey/clans")
async def api_honey_clan_create(_user: dict = Depends(require_superuser),
                                name: str = Form(...),
                                tag: str = Form(""),
                                description: str = Form("")):
    """Создать гильдию (только Матка)."""
    guild_id = get_current_guild_id(_user)
    actor = _user.get("discord_id", 0)
    clan = await he.he_clan_create(guild_id, name, tag, description, actor)
    if clan is None:
        return JSONResponse({"error": "invalid or occupied name"}, status_code=400)
    try:
        await db.g_log_activity(guild_id, "honey_clan_created", actor,
                                actor_name=_user.get("username") or _user.get("display_name"),
                                target_name=clan["name"])
    except Exception:
        pass
    return JSONResponse({"ok": True, "clan": clan})


@router.post("/api/honey/clans/{clan_id}/members")
async def api_honey_clan_member_add(clan_id: int, _user: dict = Depends(require_superuser),
                                    discord_id: int = Form(...)):
    """Добавить участника в гильдию (только Матка)."""
    guild_id = get_current_guild_id(_user)
    async with he._connect() as conn:
        async with conn.execute(
            "SELECT 1 FROM users WHERE discord_id = ?", (int(discord_id),)) as cur:
            exists = (await cur.fetchone()) is not None
    if not exists:
        return JSONResponse({"error": "user not found"}, status_code=404)
    if not await he.he_clan_member_add(guild_id, clan_id, int(discord_id)):
        return JSONResponse({"error": "already a member"}, status_code=400)
    return JSONResponse({"ok": True})


@router.delete("/api/honey/clans/{clan_id}/members/{discord_id}")
async def api_honey_clan_member_remove(clan_id: int, discord_id: int,
                                       _user: dict = Depends(require_superuser)):
    guild_id = get_current_guild_id(_user)
    if not await he.he_clan_member_remove(guild_id, clan_id, discord_id):
        return JSONResponse({"error": "not a member"}, status_code=404)
    return JSONResponse({"ok": True})


@router.delete("/api/honey/clans/{clan_id}")
async def api_honey_clan_delete(clan_id: int, _user: dict = Depends(require_superuser)):
    guild_id = get_current_guild_id(_user)
    if not await he.he_clan_delete(guild_id, clan_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})
