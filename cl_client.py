"""Async HTTP-клиент к моду ChaosLedger (Fabric, встроенный HTTP API).

Мод крутит `com.sun.net.httpserver.HttpServer` на однопоточном executor'е
`ChaosLedger-HTTP` — все запросы сериализованы. Поэтому:
  - timeout короткий (5 сек);
  - retry только для сетевых ошибок (не для 4xx — они детерминированы);
  - для realtime-панели используем bot-side snapshot-кеш TTL 10 сек, чтобы
    несколько одновременных Маток не бомбардировали мод одинаковыми запросами.

Контракт мода: API.md (ChaosLedger HTTP API). Все маршруты под `/api/*`,
auth через заголовок `X-Api-Key` (пустой ключ в config мода = open mode).
`GET /api/health` работает без ключа.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

# --- константы ---------------------------------------------------------------

DEFAULT_TIMEOUT = 5.0
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = (0.5, 1.0, 2.0)  # паузы между попытками (сек)
SNAPSHOT_TTL = 10.0  # кеш агрегированного snapshot для realtime-панели

HEALTH_PATH = "/api/health"  # без X-Api-Key

# --- исключения --------------------------------------------------------------


class CLApiError(Exception):
    """Бизнес-ошибка мода (EconomyException/StorageException/QuestException/
    RewardException) — мод вернул 400 с {error, status}."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class CLAuthError(CLApiError):
    """401 — неверный или отсутствующий X-Api-Key."""

    def __init__(self) -> None:
        super().__init__("Unauthorized (неверный X-Api-Key)", 401)


class CLUnavailableError(CLApiError):
    """Мод недоступен (connection refused / timeout после всех retry)."""

    def __init__(self, detail: str = "mod unavailable") -> None:
        super().__init__(detail, 503)


class CLConfigError(CLApiError):
    """Config мода для гильдии не задан (api_url пустой)."""

    def __init__(self, detail: str = "ChaosLedger not configured") -> None:
        super().__init__(detail, 412)


# --- клиент ------------------------------------------------------------------


class CLClient:
    """Тонкий async-клиент к HTTP API мода ChaosLedger.

    Один экземпляр на гильдию (singleton через `get_client(guild_id)`).
    `aiohttp.ClientSession` создаётся лениво и переиспользуется между вызовами.
    """

    def __init__(self, base_url: str, api_key: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        # base_url без trailing slash: http://127.0.0.1:8775
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        # snapshot-кеш для realtime-панели
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_at: float = 0.0
        self._snapshot_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession(
                        base_url=self.base_url,
                        timeout=aiohttp.ClientTimeout(total=self.timeout),
                        headers={"Accept": "application/json"},
                    )
        return self._session

    def _headers(self, *, auth: bool) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if auth and self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    async def _request(
        self, method: str, path: str, *,
        json_body: Any = None, params: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> Any:
        """Core request с retry для сетевых ошибок.

        4xx/5xx от мода — НЕ retry (детерминированы). Retry только для
        ClientError / asyncio.TimeoutError.
        """
        if not self.configured:
            raise CLConfigError()
        session = await self._ensure_session()
        last_exc: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with session.request(
                    method,
                    path,
                    json=json_body if json_body is not None else None,
                    params=params,
                    headers=self._headers(auth=auth),
                ) as resp:
                    text = await resp.text()
                    if resp.status == 401:
                        raise CLAuthError()
                    if resp.status >= 400:
                        # Мод всегда отдаёт {error, status}
                        try:
                            body = json.loads(text) if text else {}
                        except json.JSONDecodeError:
                            body = {"error": text[:200]}
                        raise CLApiError(body.get("error", f"HTTP {resp.status}"), resp.status)
                    if not text:
                        return {}
                    return json.loads(text)
            except (CLApiError, CLAuthError, CLConfigError):
                raise  # бизнес-ошибки не retry
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt + 1 < RETRY_ATTEMPTS:
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                continue
        raise CLUnavailableError(f"{last_exc}") if last_exc else CLUnavailableError()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # === Health ===

    async def health(self) -> dict[str, Any]:
        """Liveness-проба. Не требует X-Api-Key."""
        return await self._request("GET", HEALTH_PATH, auth=False)

    # === Currencies ===

    async def get_currencies(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/currencies")

    async def define_currency(self, dto: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/currencies", json_body=dto)

    # === Accounts ===

    async def get_top(self, code: str, limit: int = 10) -> list[dict[str, Any]]:
        return await self._request("GET", f"/api/accounts/top/{code}", params={"limit": limit})

    async def get_account(self, uuid: str, code: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/accounts/{uuid}/{code}")

    async def get_player_accounts(self, uuid: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/api/accounts/{uuid}")

    # === Transactions ===

    async def pay(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/transactions/pay", json_body=req)

    async def grant(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/transactions/grant", json_body=req)

    async def charge(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/transactions/charge", json_body=req)

    async def get_player_transactions(self, uuid: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self._request(
            "GET", f"/api/transactions/{uuid}", params={"limit": limit}
        )

    async def get_recent_transactions(self, code: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self._request(
            "GET", f"/api/transactions/recent/{code}", params={"limit": limit}
        )

    # === Market ===

    async def get_market(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/market")

    async def get_market_item(self, item_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/market/{item_id}")

    async def market_buy(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/market/buy", json_body=req)

    async def market_sell(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/market/sell", json_body=req)

    async def get_market_history(self, item_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self._request(
            "GET", f"/api/market/{item_id}/history", params={"limit": limit}
        )

    # === Quests ===

    async def get_quests(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/quests")

    async def get_quest(self, quest_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/quests/{quest_id}")

    async def define_quest(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/quests", json_body=req)

    async def delete_quest(self, quest_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/api/quests/{quest_id}")

    async def get_quest_progress(self, uuid: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/api/quests/progress/{uuid}")

    async def get_quest_progress_one(self, uuid: str, quest_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/quests/progress/{uuid}/{quest_id}")

    async def quest_action(self, action: str, req: dict[str, Any]) -> dict[str, Any]:
        """action in {available, start, claim, fail}."""
        return await self._request("POST", f"/api/quests/{action}", json_body=req)

    async def quest_event(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/quests/event", json_body=req)

    # === Rewards ===

    async def get_rewards(self, uuid: str, limit: int = 20) -> list[dict[str, Any]]:
        return await self._request("GET", f"/api/rewards/{uuid}", params={"limit": limit})

    async def grant_reward(self, req: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/api/rewards/grant", json_body=req)

    # === Audit ===

    async def get_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._request("GET", "/api/audit", params={"limit": limit})

    async def get_audit_actor(self, uuid: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self._request("GET", f"/api/audit/{uuid}", params={"limit": limit})

    # === Aggregated snapshot (для realtime-панели) ===========================

    async def get_ledger_state(
        self, currency: str = "CC", *, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Агрегированный snapshot для /ledger: health + top + market + recent
        transactions + audit + quests.

        Кеш TTL 10 сек: несколько одновременных Маток получают один snapshot,
        мод не бомбардируется параллельными одинаковыми запросами.
        """
        async with self._snapshot_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._snapshot is not None
                and (now - self._snapshot_at) < SNAPSHOT_TTL
            ):
                self._snapshot["cached"] = True
                return self._snapshot

            if not self.configured:
                return {"ok": False, "error": "not_configured", "cached": False}

            # 1. Health (без auth) — если мод недоступен, не дёргаем остальное.
            try:
                health = await self.health()
            except CLApiError as e:
                self._snapshot = {"ok": False, "error": "mod_unavailable", "detail": str(e)}
                self._snapshot_at = now
                return self._snapshot

            # 2. Параллельно — топ, рынок, недавние транзакции, аудит, квесты.
            # Мод однопоточный, но запросы встают в очередь executor'а — gather
            # экономит wall-clock на стороне бота.
            results = await asyncio.gather(
                self.get_top(currency, limit=10),
                self.get_market(),
                self.get_recent_transactions(currency, limit=20),
                self.get_audit(limit=20),
                self.get_quests(),
                return_exceptions=True,
            )
            top, market, txns, audit, quests = results

            def _safe(v: Any) -> Any:
                return [] if isinstance(v, Exception) else v

            state = {
                "ok": True,
                "health": health,
                "currency": currency,
                "top": _safe(top),
                "market": _safe(market),
                "transactions": _safe(txns),
                "audit": _safe(audit),
                "quests": _safe(quests),
                "fetched_at": int(time.time()),
                "cached": False,
            }
            self._snapshot = state
            self._snapshot_at = now
            return state


# --- singleton per guild -----------------------------------------------------

_clients: dict[int, CLClient] = {}
_clients_lock = asyncio.Lock()


async def get_client(guild_id: int) -> CLClient:
    """Возвращает singleton CLClient для гильдии, инициализированный из
    per-guild config (cl_data.get_cl_config). Config перечитывается при каждом
    вызове — если Матка сменила api_key в панели, новый клиент создастся на
    следующем запросе (старый закрывается)."""
    # Локальный импорт чтобы избежать circular import (cl_data импортирует
    # crypto/db, не cl_client).
    import cl_data

    cfg = await cl_data.get_cl_config(guild_id)
    new_url = (cfg.get("api_url") or "").rstrip("/")
    new_key = (cfg.get("api_key") or "").strip()

    async with _clients_lock:
        existing = _clients.get(guild_id)
        # Пересоздать клиент если config изменился или клиента ещё нет.
        if existing is None or existing.base_url != new_url or existing.api_key != new_key:
            if existing is not None:
                await existing.close()
            client = CLClient(base_url=new_url, api_key=new_key)
            _clients[guild_id] = client
            return client
        return existing


async def close_all_clients() -> None:
    async with _clients_lock:
        for client in _clients.values():
            try:
                await client.close()
            except Exception as e:
                log.warning("CLClient close failed: %s", e)
        _clients.clear()
