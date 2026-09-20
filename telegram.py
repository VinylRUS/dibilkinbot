"""
Кросс-постинг в Telegram через Bot API.
Поддержка:
  - sendMessage (текст, HTML)
  - sendPhoto (постер + HTML caption + inline кнопки + message_thread_id для тем/форумов)

ВАЖНО: все тексты в caption должны быть экранированы через tg_escape().
"""
from __future__ import annotations

import html
import json
import logging
from typing import Optional

import httpx

log = logging.getLogger("telegram")

TG_API = "https://api.telegram.org/bot{token}/{method}"


def tg_escape(text: str) -> str:
    """Экранировать спецсимволы HTML для Telegram parse_mode=HTML."""
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


async def send_message(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: Optional[str] = "HTML",
    thread_id: int | None = None,
) -> bool:
    """Отправить текстовое сообщение. Опционально в конкретную тему форума."""
    url = TG_API.format(token=token, method="sendMessage")
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread_id:
        payload["message_thread_id"] = thread_id
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("Telegram sendMessage error %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception as e:
        log.warning("Telegram sendMessage failed: %s", e)
        return False


async def send_photo(
    token: str,
    chat_id: str,
    photo_url: str,
    caption: str,
    thread_id: int | None = None,
    reply_markup: dict | None = None,
) -> bool:
    """Отправить фото с HTML-подписью и inline-кнопками.

    :param photo_url: HTTPS URL изображения (Telegram скачивает сам)
    :param caption: HTML-форматированный текст (макс. 1024 символа)
    :param thread_id: ID темы форума (None → General topic или обычный чат)
    :param reply_markup: inline_keyboard dict, например:
        {"inline_keyboard": [[{"text": "Кнопка", "url": "https://..."}]]}
    :return: True при успехе
    """
    url = TG_API.format(token=token, method="sendPhoto")
    payload: dict = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption[:1024],  # жёсткий лимит Telegram
        "parse_mode": "HTML",
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("Telegram sendPhoto error %s: %s", resp.status_code, resp.text[:300])
            # Fallback: пробуем sendMessage без фото
            log.info("Falling back to sendMessage (no photo)")
            return await send_message(token, chat_id, caption, thread_id=thread_id)
        return True
    except Exception as e:
        log.warning("Telegram sendPhoto failed: %s", e)
        return False


async def create_forum_topic(
    token: str,
    chat_id: str,
    name: str,
    icon_color: int = 0x6FB9F0,  # blue
) -> int | None:
    """Создать тему в форуме. Возвращает message_thread_id, или None при ошибке.

    Требуется, что бот — админ с правом can_manage_topics.
    icon_color: один из 0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F.
    """
    url = TG_API.format(token=token, method="createForumTopic")
    payload = {"chat_id": chat_id, "name": name, "icon_color": icon_color}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            log.warning("Telegram createForumTopic error %s: %s", resp.status_code, resp.text[:300])
            return None
        data = resp.json()
        return data.get("result", {}).get("message_thread_id")
    except Exception as e:
        log.warning("Telegram createForumTopic failed: %s", e)
        return None
