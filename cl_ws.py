"""WebSocket reverse-tunnel server: принимает входящее подключение от мода
ChaosLedger, который крутится на shared Minecraft-хостинге (Exaroton/Aternos).

Архитектура (см. WS_TUNNEL_PROTOCOL.md):
  - Exaroton открывает наружу только Minecraft-порт (25565). Любой другой порт,
    на котором мод поднял бы HTTP API, слушает на localhost и недоступен боту.
  - Exaroton НЕ режет исходящие. Поэтому соединение разворачивается наоборот:
    мод сам подключается к боту как WS-клиент на /ws/chaosledger.

Трафик через одно WS-соединение:
  - mod → bot: periodic snapshot (push каждые N сек)
  - bot → mod: request.* (команды pay/grant/charge/market_*/quest_*/...)
  - mod → bot: reply.* (ответы на команды, корреляция по id)

Все настройки (token, server_id, enabled) хранятся в БД и редактируются через
/adminpanel бота (вкладки «Токены», «Каналы», «Фичи»). Ничего не в env.

Трединг: обработка входящих сообщений — на event loop uvicorn'а. Команды
бот отправляет через `send_command()` — future по id, awaitится вызывающим
кодоном (cl_commands.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import cl_cache
import crypto
import db

log = logging.getLogger(__name__)

router = APIRouter()

PROTOCOL_VERSION = 1
HANDSHAKE_TIMEOUT = 10.0  # сек — мод должен прислать hello за это время
COMMAND_TIMEOUT = 8.0      # сек — ждём reply.* от мода

# WS close codes (см. протокол §6)
CLOSE_NORMAL = 1000
CLOSE_UNAUTHORIZED = 4001
CLOSE_PROTOCOL_VERSION = 4002
CLOSE_INTERNAL = 4500


class WsTunnelError(Exception):
    """Локальная ошибка туннеля (мод не подключен, timeout, ...)."""


class WsCommandError(Exception):
    """Бизнес-ошибка от мода (EconomyException и т.д.) — пробрасывается как
    CLApiError-эквивалент для cl_commands.py."""

    def __init__(self, message: str, error_code: str = "INTERNAL", status: int = 400) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.status = status


# --- ошибки → HTTP-статусы для cl_commands -----------------------------

_ERROR_STATUS = {
    "BAD_REQUEST": 400,
    "ECONOMY": 400,
    "STORAGE": 400,
    "QUEST": 400,
    "REWARD": 400,
    "INTERNAL": 500,
    "UNKNOWN_ACTION": 400,
    "UNAUTHORIZED": 401,
    "PROTOCOL_VERSION": 400,
}


class _TunnelConnection:
    """Активное соединение с модом. Singleton — мод один, сервер один.

    Заменяется при переподключении мода. Доступ — через get_active().
    """

    def __init__(self, ws: WebSocket, server_id: str) -> None:
        self.ws = ws
        self.server_id = server_id
        self.connected_at = time.time()
        self.last_snapshot_at = 0.0
        self.last_snapshot_seq = 0
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def send_command(self, action: str, payload: dict[str, Any]) -> Any:
        """Отправить request.<action>, дождаться reply.<action>.

        Поднимает WsTunnelError если мод не подключен / timeout.
        Поднимает WsCommandError если мод вернул ok=false.
        """
        cmd_id = str(uuid.uuid4())
        msg = {
            "v": PROTOCOL_VERSION,
            "type": f"request.{action}",
            "id": cmd_id,
            "ts": int(time.time() * 1000),
            "data": payload,
        }
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[cmd_id] = future
        try:
            await self.ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception as e:
            self._pending.pop(cmd_id, None)
            raise WsTunnelError(f"WS send failed: {e}") from e
        try:
            return await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise WsTunnelError(f"timeout waiting reply for {action}")
        except WsCommandError:
            raise
        except Exception as e:
            self._pending.pop(cmd_id, None)
            raise WsTunnelError(f"command {action} failed: {e}") from e

    def resolve_reply(self, cmd_id: str, ok: bool, result: Any, error: str, message: str) -> None:
        """Разрешить pending future по пришедшему reply."""
        fut = self._pending.pop(cmd_id, None)
        if fut is None:
            log.warning("reply for unknown cmd_id=%s (already timed out?)", cmd_id)
            return
        if fut.done():
            return
        if ok:
            fut.set_result(result)
        else:
            status = _ERROR_STATUS.get(error, 400)
            fut.set_exception(WsCommandError(message or error, error, status))

    def fail_all(self, reason: str) -> None:
        """Мод отключился — завершить все pending futures ошибкой."""
        for cmd_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(WsTunnelError(reason))
        self._pending.clear()


# --- активное соединение (singleton) -----------------------------------------

_active: Optional[_TunnelConnection] = None
_active_lock = asyncio.Lock()


async def get_active() -> Optional[_TunnelConnection]:
    """Текущее активное соединение с модом или None если мод не подключен."""
    return _active


async def _set_active(conn: Optional[_TunnelConnection]) -> None:
    global _active
    async with _active_lock:
        if _active is not None and conn is not None:
            # Мод переподключился, не закрыв старое соединение — корректно
            # заменяем; старые pending фейлим.
            log.warning("mod reconnected without closing previous WS — replacing")
            _active.fail_all("mod reconnected")
        _active = conn


async def send_command(action: str, payload: dict[str, Any]) -> Any:
    """Публичный entrypoint для cl_commands: отправить команду моду.

    Поднимает WsTunnelError если мод не подключен.
    """
    conn = await get_active()
    if conn is None:
        raise WsTunnelError("мод ChaosLedger не подключен (ожидайте reverse-tunnel)")
    return await conn.send_command(action, payload)


def is_connected() -> bool:
    return _active is not None


# --- WS endpoint -------------------------------------------------------------

@router.websocket("/ws/chaosledger")
async def ws_chaosledger(websocket: WebSocket) -> None:
    """Endpoint reverse-tunnel. Мод подключается сюда, шлёт hello с токеном.

    Бот валидирует токен против DB setting `cl_ws_token`, отвечает hello_ack и
    затем принимает snapshot'ы / request'ы.
    """
    # Принимаем на уровне WS — без FastAPI Depends, т.к. WS-клиент это мод,
    # а не браузер с сессией.
    enabled = (await db.get_setting("cl_ws_enabled")) == "1"
    if not enabled:
        await websocket.close(code=CLOSE_NORMAL, reason="ChaosLedger disabled in /adminpanel → Фичи")
        return
    expected_token_enc = await db.get_setting("cl_ws_token")
    expected_token = crypto.decrypt(expected_token_enc) if expected_token_enc else ""
    if not expected_token:
        log.warning("WS /ws/chaosledger: cl_ws_token not set in /adminpanel → Токены. Closing.")
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="cl_ws_token not configured")
        return

    await websocket.accept()

    # 1. Ждём hello от мода (с таймаутом).
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=HANDSHAKE_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="hello timeout")
        return

    try:
        hello = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="hello not JSON")
        return

    if hello.get("type") != "hello":
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="first message must be hello")
        return

    data = hello.get("data", {}) or {}
    token = data.get("token", "")
    if not isinstance(token, str) or token != expected_token:
        log.warning("WS /ws/chaosledger: bad token (got %d chars)", len(token) if token else 0)
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="bad token")
        return

    proto = data.get("protocol", 1)
    if proto != PROTOCOL_VERSION:
        await websocket.close(code=CLOSE_PROTOCOL_VERSION,
                               reason=f"protocol v{proto} != {PROTOCOL_VERSION}")
        return

    server_id = data.get("serverId", "(unknown)")
    mod_version = data.get("modVersion", "?")
    log.info("CL WS: мод подключился serverId=%s modVersion=%s", server_id, mod_version)

    # 2. hello_ack — подтверждаем готовность принимать snapshot'ы и команды.
    ack = {
        "v": PROTOCOL_VERSION,
        "type": "hello_ack",
        "ts": int(time.time() * 1000),
        "data": {
            "botVersion": "3.14.0",
            "serverTime": int(time.time() * 1000),
            "snapshotIntervalSec": 10,
        },
    }
    await websocket.send_text(json.dumps(ack, ensure_ascii=False))

    conn = _TunnelConnection(websocket, server_id)
    await _set_active(conn)
    cl_cache.mark_connected(server_id)

    # 3. Message loop.
    try:
        while True:
            msg_raw = await websocket.receive_text()
            try:
                msg = json.loads(msg_raw)
            except json.JSONDecodeError:
                log.warning("CL WS: non-JSON message from mod (%d bytes)", len(msg_raw))
                continue
            mtype = msg.get("type", "")
            if mtype == "snapshot":
                await cl_cache.update(msg.get("data", {}), conn)
            elif mtype == "ping":
                await websocket.send_text(json.dumps({
                    "v": PROTOCOL_VERSION, "type": "pong",
                    "ts": int(time.time() * 1000), "data": {},
                }, ensure_ascii=False))
            elif mtype.startswith("reply."):
                action = mtype[len("reply."):]
                cmd_id = msg.get("id", "")
                rdata = msg.get("data", {}) or {}
                conn.resolve_reply(
                    cmd_id,
                    ok=bool(rdata.get("ok")),
                    result=rdata.get("result"),
                    error=rdata.get("error", "INTERNAL"),
                    message=rdata.get("message", ""),
                )
            else:
                log.debug("CL WS: ignoring message type=%s", mtype)
    except WebSocketDisconnect:
        log.info("CL WS: мод отключился (serverId=%s)", server_id)
    except Exception as e:
        log.error("CL WS: handler error: %s", e, exc_info=True)
    finally:
        conn.fail_all("mod disconnected")
        cl_cache.mark_disconnected()
        await _set_active(None)
        try:
            await websocket.close()
        except Exception:
            pass
