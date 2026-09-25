"""
Pointauc Lots Watcher: поллинг list_lots() с автоматическим определением
состояния "стример онлайн/офлайн".

Ключевое: GET /api/oshino/lots ВИСНЕТ если Pointauc client стримера не подключён.
Сервер держит HTTP-соединение открытым в ожидании ответа от клиента стримера.
Поэтому:
- ReadTimeout = стример офлайн (нормально, не ошибка)
- 200 OK = стример онлайн, можно ловить изменения лотов
- 401/403 = проблема с токеном (не восстановится сама)
- Другие RequestError = сетевая проблема, retry с backoff

State-машина:
- online: polling каждые 5 секунд, diff'аем лоты
- offline: polling каждые 2 минуты (тихо), ждём пока стример запустит client
- При смене состояния логируем один раз (не спамим)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

import db
import pointauc
from pointauc import PointaucClient, PointaucError, PointaucStreamerOffline

log = logging.getLogger("pointauc_watcher")

# Интервалы опроса
POLL_ONLINE = 5.0       # стример онлайн — частый опрос
POLL_OFFLINE = 120.0    # стример офлайн — редкий опрос (проверить не появился ли)
POLL_NO_TOKEN = 60.0    # токен не задан — редкий опрос (вдруг зададут через панель)


class LotsWatcher:
    """Смотрит за изменениями в list_lots() и эмитит события исчезновения лотов."""

    def __init__(self, on_lot_removed: Callable[[str, str | None], Awaitable[None]]):
        """
        :param on_lot_removed: async callback(lot_name, lot_id) — вызывается при исчезновении лота.
        """
        self._on_lot_removed = on_lot_removed
        self._known_lots: dict[str, str] = {}  # lot_id → lot_name
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_error: str | None = None

        # State machine
        self._streamer_online: bool | None = None  # None = unknown (старт)
        self._token_ok: bool | None = None  # None = unknown

    async def _fetch_lots(self, client: PointaucClient) -> list[dict] | None:
        """Возвращает список лотов или None если не вышло.

        Дополнительно обновляет state-машину:
        - PointaucStreamerOffline → _streamer_online = False
        - 401/403 → _token_ok = False
        - Успех → _streamer_online = True, _token_ok = True
        """
        try:
            lots = await client.list_lots()
            # Успех — стример онлайн, токен валиден
            self._on_success()
            return lots
        except PointaucStreamerOffline:
            self._on_streamer_offline()
            return None
        except PointaucError as e:
            msg = str(e)
            if "Personal Token" in msg:
                self._on_token_invalid()
            elif self._last_error != msg:
                log.warning("Pointauc fetch failed: %s", msg)
            self._last_error = msg
            return None

    def _on_success(self) -> None:
        """Обновить состояние при успешном запросе."""
        self._last_error = None
        if self._streamer_online is not True:
            # Переход: offline/unknown → online
            log.info("✓ Pointauc streamer online — polling every %.0fs", POLL_ONLINE)
            self._streamer_online = True
        self._token_ok = True

    def _on_streamer_offline(self) -> None:
        """Обновить состояние когда стример офлайн (ReadTimeout)."""
        if self._streamer_online is not False:
            # Переход: online/unknown → offline
            log.info("💤 Pointauc streamer offline — polling every %.0fs (это нормально, ждём пока стример запустит client)",
                     POLL_OFFLINE)
            self._streamer_online = False
            # Сбрасываем кеш лотов — при следующем онлайн они могут быть другими
            self._known_lots = {}

    def _on_token_invalid(self) -> None:
        """Обновить состояние когда токен невалиден (401/403)."""
        if self._token_ok is not False:
            log.warning("❌ Pointauc token invalid или истёк — задайте новый в админ-панели /tokens")
            self._token_ok = False

    async def _tick(self, client: PointaucClient) -> None:
        """Один шаг опроса."""
        lots = await self._fetch_lots(client)
        if lots is None:
            return

        # Нормализуем
        current: dict[str, str] = {}
        for lot in lots:
            lot_id = str(lot.get("id") or lot.get("Id") or "")
            lot_name = lot.get("name") or lot.get("Name") or "—"
            if lot_id:
                current[lot_id] = lot_name

        # Bulk clear: все лоты исчезли одновременно → это "clear all", не winner
        if not current and self._known_lots:
            log.info("All lots cleared (was %d) — treating as bulk clear, not winner", len(self._known_lots))
            self._known_lots = {}
            return

        # Найти исчезнувшие лоты
        removed_ids = set(self._known_lots.keys()) - set(current.keys())
        for lot_id in removed_ids:
            lot_name = self._known_lots[lot_id]
            log.info("Lot removed: id=%s name='%s' (potential winner)", lot_id, lot_name)
            try:
                await self._on_lot_removed(lot_name, lot_id)
            except Exception as e:
                log.error("on_lot_removed callback failed: %s", e, exc_info=True)

        # Обновить кеш (включая renames — если lot_id остался, но name поменялся)
        self._known_lots = current

    def _next_sleep(self) -> float:
        """Сколько ждать до следующего тика, в зависимости от состояния."""
        if self._token_ok is False:
            return POLL_NO_TOKEN
        if self._streamer_online is False:
            return POLL_OFFLINE
        return POLL_ONLINE

    async def _run_loop(self, get_client: Callable[[], Awaitable[PointaucClient | None]]) -> None:
        log.info("LotsWatcher started (online=%.0fs, offline=%.0fs, no_token=%.0fs)",
                 POLL_ONLINE, POLL_OFFLINE, POLL_NO_TOKEN)
        while self._running:
            client = await get_client()
            if client is None:
                # Токен не задан — ждём подольше, не спамим лог
                if self._token_ok is not False:
                    log.info("⏳ Pointauc token not configured — waiting for admin to set it via /tokens")
                    self._token_ok = False
                await asyncio.sleep(POLL_NO_TOKEN)
                continue

            try:
                await self._tick(client)
            except Exception as e:
                log.error("LotsWatcher tick unexpected error: %s", e, exc_info=True)

            await asyncio.sleep(self._next_sleep())
        log.info("LotsWatcher stopped")

    def start(self, get_client: Callable[[], Awaitable[PointaucClient | None]]) -> None:
        if self._task is not None and not self._task.done():
            log.warning("LotsWatcher already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(get_client))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_known_lots(self) -> dict[str, str]:
        """Снапшот текущих лотов (для /wheel list из кеша)."""
        return dict(self._known_lots)

    def get_status(self) -> dict:
        """Текущее состояние для отображения в панели."""
        return {
            "streamer_online": self._streamer_online,
            "token_ok": self._token_ok,
            "known_lots_count": len(self._known_lots),
            "next_poll_interval": self._next_sleep(),
        }
