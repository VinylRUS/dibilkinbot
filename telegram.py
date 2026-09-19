"""
Кросс-постинг в Telegram через Bot API.
Только отправка сообщений (sendMessage), без приёма обновлений.

ВАЖНО: все тексты должны быть экранированы для HTML, т.к. parse_mode=HTML.
Используй tg_html_escape() для пользовательского ввода перед вставкой в шаблон.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

import httpx

log = logging.getLogger("telegram")

TG_API = "https://api.telegram.org/bot{token}/{method}"


def tg_escape(text: str) -> str:
    """Экранировать спецсимволы HTML для Telegram parse_mode=HTML.

    Telegram поддерживает ОГРАНИЧЕННЫЙ набор тегов: <b>, <i>, <u>, <s>, <code>, <pre>, <a>.
    Любой < или >, не относящийся к этим тегам, ломает парсинг → 400 Bad Request.
    Поэтому экранируем все спецсимволы в пользовательском тексте.

    Пример: <@123> → &lt;@123&gt; (Telegram отрендерит как текст "<@123>")
    """
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


async def send_message(token: str, chat_id: str, text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """
    Отправить сообщение в Telegram-чат.
    :return: True при успехе, False при ошибке (не кидает — бот не должен падать из-за ТГ).
    """
    url = TG_API.format(token=token, method="sendMessage")
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("Telegram error %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False
