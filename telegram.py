"""
Кросс-постинг в Telegram через Bot API.
Только отправка сообщений (sendMessage), без приёма обновлений.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("telegram")

TG_API = "https://api.telegram.org/bot{token}/{method}"


async def send_message(token: str, chat_id: str, text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """
    Отправить сообщение в Telegram-чат.
    :return: True при успехе, False при ошибке (не кидает — бот не должен падать из-за ТГ).
    """
    url = TG_API.format(token=token, method="sendMessage")
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("Telegram error %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False
