"""
Pointauc Lots Watcher: поллинг list_lots() каждые N секунд, дифф по lot_id,
эмитит события исчезновения лотов (потенциальные победители).

Стратегия для режима Simple (без provably-fair):
- Раз в 5 секунд опрашиваем GET /api/oshino/lots
- Храним в памяти {lot_id → lot_name}
- Если лот исчез — это либо spin (победитель), либо manual delete (различить нельзя)
- Эмитим событие 'lot_removed' с пометкой confidence='unconfirmed'
- Bulk clear (len(new)==0 && len(old)>1) — отдельное событие 'cleared', не winner
- Streamer offline (HTTP 500) — пропускаем тик
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

import db
import pointauc
from pointauc import PointaucClient, PointaucError

log = logging.getLogger("pointauc_watcher")

POLL_INTERVAL = 5.0  # секунд между успешными запросами
ERROR_BACKOFF = 30.0  # секунд ждать если была ошибка (ReadTimeout, ConnectError и т.п.)


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
        self._consecutive_errors = 0

    async def _fetch_lots(self, client: PointaucClient) -> list[dict] | None:
        """Возвращает список лотов или None если не вышло (стример офлайн, нетокен, и т.д.)."""
        try:
            return await client.list_lots()
        except PointaucError as e:
            msg = str(e)
            # Если предыдущая ошибка была другая — логируем
            if self._last_error != msg:
                log.warning("Pointauc fetch failed: %s", msg)
                self._last_error = msg
            return None

    async def _tick(self, client: PointaucClient) -> bool:
        """Один шаг опроса. Возвращает True если успешно, False если была ошибка."""
        lots = await self._fetch_lots(client)
        if lots is None:
            return False

        # Сбрасываем флаг последней ошибки если успешно
        self._last_error = None

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
            return True

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
        return True

    async def _run_loop(self, get_client: Callable[[], Awaitable[PointaucClient | None]]) -> None:
        log.info("LotsWatcher started, polling every %.1fs (backoff %.1fs on errors)",
                 POLL_INTERVAL, ERROR_BACKOFF)
        while self._running:
            client = await get_client()
            if client is None:
                # Токен не задан — ждём подольше, не спамим лог
                await asyncio.sleep(30)
                continue
            try:
                success = await self._tick(client)
                if success:
                    self._consecutive_errors = 0
                    await asyncio.sleep(POLL_INTERVAL)
                else:
                    # Ошибка — backoff растёт с количеством последовательных ошибок
                    self._consecutive_errors += 1
                    backoff = min(ERROR_BACKOFF * self._consecutive_errors, 120)  # кап 2 минуты
                    log.info("Backing off %.0fs before next poll (consecutive_errors=%d)",
                             backoff, self._consecutive_errors)
                    await asyncio.sleep(backoff)
            except Exception as e:
                log.error("LotsWatcher tick unexpected error: %s", e, exc_info=True)
                await asyncio.sleep(ERROR_BACKOFF)
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
