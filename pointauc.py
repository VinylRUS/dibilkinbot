"""
Тонкий HTTP-клиент к Pointauc API.
Endpoint'ы взяты из официальной .NET-либы Pointauc.Api (https://github.com/Pointauc/Pointauc.Api).

Используем 2 из 4 методов:
  - POST /api/oshino/bids     — добавить пункт в колесо (bid)
  - GET  /api/oshino/lots      — получить все текущие лоты (пункты колеса)

Остальные методы ChangeLot (PUT) и GetBidStatus (GET) нам не нужны для MVP.
"""
from __future__ import annotations

import httpx

POINTAUC_BASE = "https://pointauc.com/api/oshino"


class PointaucError(Exception):
    pass


class PointaucClient:
    def __init__(self, token: str, timeout: float = 15.0):
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def add_bid(self, message: str, cost: int = 0) -> list[str]:
        """
        Добавить пункт в колесо.
        :param message: название фильма (отображается на колесе)
        :param cost: стоимость ставки. 0 валиден в исходной .NET-либе,
                     серверная политика списания не задокументирована (см. README ТЗ).
        :return: список ID созданных bid'ов.
        :raises PointaucError: при HTTP-ошибке или проблеме с авторизацией.
        """
        payload = {
            "bids": [
                {
                    "message": message,
                    "cost": cost,
                    "insertStrategy": "Force",  # создать новый lot, а не дописывать в существующий
                }
            ]
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    f"{POINTAUC_BASE}/bids",
                    json=payload,
                    headers=self._headers(),
                )
            except httpx.RequestError as e:
                raise PointaucError(f"Pointauc недоступен: {e}") from e

            if resp.status_code in (401, 403):
                raise PointaucError("Pointauc: неверный или истёкший Personal Token")
            if resp.status_code >= 400:
                raise PointaucError(
                    f"Pointauc: HTTP {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            if not isinstance(data, list):
                raise PointaucError(f"Pointauc: неожиданный ответ: {data}")
            return data

    async def list_lots(self) -> list[dict]:
        """
        Получить все текущие лоты (пункты колеса).
        :return: список словарей {id, name, amount, fastId, investors}
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    f"{POINTAUC_BASE}/lots",
                    headers=self._headers(),
                )
            except httpx.RequestError as e:
                raise PointaucError(f"Pointauc недоступен: {e}") from e

            if resp.status_code in (401, 403):
                raise PointaucError("Pointauc: неверный или истёкший Personal Token")
            if resp.status_code >= 400:
                raise PointaucError(
                    f"Pointauc: HTTP {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            lots = data.get("lots", []) if isinstance(data, dict) else []
            return lots
