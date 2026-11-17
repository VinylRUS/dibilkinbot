"""In-memory cache состояния ChaosLedger, обновляемый из snapshot'ов мода.

Бот больше не опрашивает мод по HTTP каждые 10 секунд — вместо этого мод сам
push'ит snapshot через WS reverse-tunnel каждые ~10 секунд. Этот модуль
хранит последний snapshot и отдаёт его /api/ledger/state и cl_commands без
обращений к моду.

Threading: данные обновляются из cl_ws.py (однопоточный event loop uvicorn'а).
Чтение — из FastAPI handlers в том же loop. asyncio.Lock не нужен, т.к.
asyncio однопоточный; но мы используем простую защиту от race при
перезаписи (snapshot приходит целиком, атомарно через await).

Структура cache совместима со схемой get_ledger_state() из cl_client.py,
чтобы templates/ledger.html не пришлось переписывать.
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# In-memory state — singleton для процесса бота.
_state: dict[str, Any] = {
    "ok": False,
    "connected": False,
    "health": None,
    "currency": None,
    "currencies": [],
    "accounts": [],
    "top": [],
    "market": [],
    "transactions": [],
    "audit": [],
    "quests": [],
    "fetched_at": 0,
    "snapshot_seq": 0,
    "last_error": None,
    "server_id": None,
    "mod_version": None,
}

# Snapshot собирается модом и хранит `top` (оттуда берём «top») и `accounts`
# (полный список). templates/ledger.html читает top → для совместимости
# копируем top из accounts если мод не прислал отдельное поле top.


def mark_connected(server_id: str) -> None:
    _state["connected"] = True
    _state["server_id"] = server_id
    _state["last_error"] = None
    log.info("CL cache: connected (server_id=%s)", server_id)


def mark_disconnected() -> None:
    _state["connected"] = False
    _state["ok"] = False
    _state["last_error"] = "мод отключился от reverse-tunnel"
    log.info("CL cache: disconnected")


def update(snapshot: dict[str, Any], conn: Any = None) -> None:
    """Заменить кеш новым snapshot'ом от мода.

    Snapshot от мода содержит:
      seq, currency, currencies[], accounts[], market[], transactions[],
      quests[], audit[] (см. WsTunnelClient.buildSnapshot).
    Для совместимости со старым cl_client.get_ledger_state(), который
    возвращал отдельное поле `top`, копируем accounts → top.
    """
    if not isinstance(snapshot, dict):
        log.warning("CL cache: snapshot is not a dict (got %s)", type(snapshot).__name__)
        return
    now = int(time.time())
    accounts = snapshot.get("accounts", []) or []
    currencies = snapshot.get("currencies", []) or []
    market = snapshot.get("market", []) or []
    transactions = snapshot.get("transactions", []) or []
    quests = snapshot.get("quests", []) or []
    audit = snapshot.get("audit", []) or []
    currency = snapshot.get("currency")
    seq = int(snapshot.get("seq", 0) or 0)

    _state.update({
        "ok": True,
        "health": {
            "status": "ok",
            "mod": "ChaosLedger",
            "version": _state.get("mod_version") or "0.2.0",
            "serverTime": now * 1000,
        },
        "currency": currency,
        "currencies": currencies,
        "accounts": accounts,
        "top": accounts,  # мод уже отдаёт топ-N по умолчанию
        "market": market,
        "transactions": transactions,
        "audit": audit,
        "quests": quests,
        "fetched_at": now,
        "snapshot_seq": seq,
        "last_error": None,
    })
    if conn is not None:
        conn.last_snapshot_at = time.time()
        conn.last_snapshot_seq = seq


def get_state(currency: str | None = None) -> dict[str, Any]:
    """Вернуть текущий snapshot для /api/ledger/state.

    Совместимо со схемой cl_client.get_ledger_state() — templates/ledger.html
    не меняется. Если мод не подключен, ok=False + last_error.
    """
    if not _state.get("connected"):
        return {
            "ok": False,
            "error": "mod_disconnected",
            "detail": _state.get("last_error") or "мод не подключен",
            "cached": False,
            "connected": False,
        }
    # Возвращаем копию (безопасно — templates/serializers не мутируют).
    return {
        "ok": True,
        "health": _state.get("health"),
        "currency": _state.get("currency") or currency,
        "currencies": _state.get("currencies", []),
        "top": _state.get("top", []),
        "market": _state.get("market", []),
        "transactions": _state.get("transactions", []),
        "audit": _state.get("audit", []),
        "quests": _state.get("quests", []),
        "fetched_at": _state.get("fetched_at", 0),
        "snapshot_seq": _state.get("snapshot_seq", 0),
        "connected": True,
        "server_id": _state.get("server_id"),
        "cached": True,  # всегда из кеша (realtime push, не pull)
    }


def get_health() -> dict[str, Any]:
    """Лайвнесс для индикатора в шапке /ledger."""
    if not _state.get("connected"):
        return {"ok": False, "error": "mod_disconnected"}
    return {"ok": True, "health": _state.get("health")}


def get_currency_default() -> str | None:
    """Код валюты по умолчанию из последнего snapshot'а (CC = медяки)."""
    return _state.get("currency")


def get_currency_meta(code: str | None = None) -> dict[str, Any] | None:
    """Метаданные валюты (displayName, symbol) из snapshot'а."""
    target = code or _state.get("currency")
    if not target:
        return None
    for c in _state.get("currencies", []):
        if c.get("code") == target:
            return c
    return None
