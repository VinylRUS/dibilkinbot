"""FastAPI APIRouter — маршруты «Мёда» 🍯 v3.15.

Содержит:
- HTML-страницу /honey (уровень, подарки, лотерея, ставки, рамки, гильдии, банк)
- REST API:
  - POST /api/honey/gift          (#31: P2P-перевод с комиссией 10% → банк)
  - GET  /api/honey/level         (#52: уровень и прогресс-бар)
  - POST /api/honey/frame/{key}   (#27: статичные рамки, 100 мёда)
  - POST /api/honey/gifframe/{key}(#45: GIF-рамки, 200 мёда — финальный этап)
  - GET  /api/honey/bank + POST /api/honey/bank/adjust (только Матка)
  - GET  /api/honey/lottery + POST /api/honey/lottery/buy (#14)
  - GET  /api/honey/bets + POST /api/honey/bets/place (#15)
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
    """Хаб экономики «Мёд»: уровень, подарки, лотерея, ставки, магазин рамок,
    гильдии, банк (банковский блок — только для Матки)."""
    guild_id = get_current_guild_id(_user)
    uid = _user.get("discord_id", 0)
    is_superuser = bool(_user.get("is_superuser"))

    balance = await db.g_honey_balance(guild_id, uid) if uid else 0
    level = await he.he_level(guild_id, uid) if uid else None
    lottery = await he.he_lottery_info(guild_id, uid) if uid else \
        await he.he_lottery_info(guild_id)
    bets = await he.he_bets_open(guild_id)
    bets_history = await he.he_bets_history(guild_id, uid, limit=10) if uid else []
    frames = await he.he_frames_catalog(guild_id, uid) if uid else None
    clans = await he.he_clan_list(guild_id)
    wheel_items = await db.g_list_wheel_items(guild_id, active_only=True)

    bank = None
    bank_log = []
    if is_superuser:
        bank = await he.he_bank_balance(guild_id)
        bank_log = await he.he_bank_log(guild_id, limit=25)

    return templates.TemplateResponse(request, "honey.html", {
        "user": _user,
        "balance": balance,
        "level": level,
        "lottery": lottery,
        "lottery_last": await he.he_lottery_last(guild_id),
        "bets": bets,
        "bets_history": bets_history,
        "frames": frames,
        "clans": clans,
        "wheel_items": [{"id": i["id"], "name": i["name"], "color": i["color"]}
                        for i in wheel_items],
        "bank": bank,
        "bank_log": bank_log,
        "is_superuser": is_superuser,
        "gift_commission": he.HONEY_GIFT_COMMISSION,
        "bee_reward": he.HONEY_BEE_REWARD,
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
    """Поставить мёд на сектор. Окно приёма — пока колесо не крутится;
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

    # Окно приёма ставок: не во время крутки и не после недавнего победителя
    # (победитель уже определён — до нового сбора крутки невозможны).
    import wheel_routes
    lock = wheel_routes._wheel_spin_locks.get(int(guild_id))
    if lock is not None and lock.locked():
        return JSONResponse({"error": "Колесо крутится — ставки закрыты до результата.",
                             "error_code": "spin_in_progress"}, status_code=409)
    recent_winner = await db.g_get_recent_winner(guild_id, since_minutes=60)
    if recent_winner:
        return JSONResponse({"error": "Победитель уже определён — дождитесь нового сбора.",
                             "error_code": "winner_recent"}, status_code=409)

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
