"""
WebSocket manager для реал-тайм обновлений колеса.
Подключения: стример (браузер на /wheel) + теоретически Discord-бот мог бы слать события.

События (broadcast всем подписчикам):
  - wheel_updated: список лотов изменился (add/remove/clear)
  - spin_started: стример запустил спин (показать анимацию)
  - spin_result: победитель определён (финал анимации)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket

log = logging.getLogger("ws")

# Активные WebSocket-подключения
_active_connections: Set[WebSocket] = set()


async def connect(websocket: WebSocket) -> None:
    """Принять новое подключение."""
    await websocket.accept()
    _active_connections.add(websocket)
    log.info("WS connected, total: %d", len(_active_connections))


async def disconnect(websocket: WebSocket) -> None:
    """Удалить подключение при отключении."""
    _active_connections.discard(websocket)
    log.info("WS disconnected, total: %d", len(_active_connections))


async def broadcast(event_type: str, payload: dict) -> None:
    """Разослать событие всем подключённым клиентам."""
    if not _active_connections:
        return
    message = json.dumps({"type": event_type, "payload": payload}, ensure_ascii=False)
    # Копируем множество — может измениться во время итерации
    dead: list[WebSocket] = []
    for ws in list(_active_connections):
        try:
            await ws.send_text(message)
        except Exception as e:
            log.debug("WS send failed, will remove: %s", e)
            dead.append(ws)
    for ws in dead:
        _active_connections.discard(ws)


async def broadcast_wheel_updated(items: list[dict]) -> None:
    """Сообщить клиентам что список лотов обновился."""
    await broadcast("wheel_updated", {"items": items, "count": len(items)})


async def broadcast_spin_started(items: list[dict], spin_id: str) -> None:
    """Сообщить клиентам что спин начался (можно крутить анимацию)."""
    await broadcast("spin_started", {"items": items, "spin_id": spin_id})


async def broadcast_spin_result(winner: dict, spin_id: str) -> None:
    """Сообщить клиентам что спин завершён, победитель известен."""
    await broadcast("spin_result", {"winner": winner, "spin_id": spin_id})
