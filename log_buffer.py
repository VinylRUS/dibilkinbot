"""v2.4.3: In-memory ring buffer для логов с timestamp.

Хранит последние N записей за 24 часа. Используется командой /logs
(ответ бота в Telegram) чтобы дать Матке последние логи без доступа к серверу.

Подключается к logging через MemoryLogHandler. Буфер ограничен:
- По времени: 24 часа (устаревшие записи удаляются)
- По размеру: 5000 записей (макс) — чтобы не жрать память

Формат записей в буфере:
  {timestamp, level, logger_name, message}

Потокобезопасный — использует asyncio.Lock для записи/чтения.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone

# 24 часа в секундах
_RETENTION_SECONDS = 24 * 3600
# Максимальный размер буфера (защита от ООМ при всплеске логов)
_MAX_ENTRIES = 5000


class _LogEntry:
    """Одна запись лога в буфере."""
    __slots__ = ("ts", "level", "level_name", "logger_name", "message")

    def __init__(self, ts: float, level: int, level_name: str,
                 logger_name: str, message: str):
        self.ts = ts
        self.level = level
        self.level_name = level_name
        self.logger_name = logger_name
        self.message = message

    def format(self) -> str:
        """Строка для вывода в /logs."""
        dt = datetime.fromtimestamp(self.ts, tz=timezone.utc).astimezone()
        return f"{dt.strftime('%H:%M:%S')} [{self.level_name}] {self.logger_name}: {self.message}"


class MemoryLogHandler(logging.Handler):
    """logging.Handler который хранит записи в памяти (ring buffer).

    Потокобезопасный. Не блокирует event loop (только лок на append).
    """
    def __init__(self, max_entries: int = _MAX_ENTRIES,
                 retention_seconds: int = _RETENTION_SECONDS):
        super().__init__(level=logging.INFO)  # INFO и выше
        self._buffer: deque[_LogEntry] = deque(maxlen=max_entries)
        self._retention = retention_seconds
        self._lock = asyncio.Lock()
        # Храним время последней cleanup — чтобы не гонять cleanup на каждой записи
        self._last_cleanup = time.time()

    def emit(self, record: logging.LogRecord) -> None:
        """Вызывается synchronously из logging — нельзя делать async."""
        try:
            # Не используем asyncio.Lock здесь — emit синхронный.
            # deque потокобезопасен для append/popleft в CPython (GIL).
            entry = _LogEntry(
                ts=record.created,
                level=record.levelno,
                level_name=record.levelname,
                logger_name=record.name,
                message=record.getMessage(),
            )
            self._buffer.append(entry)
            # Периодически чистим устаревшие — не на каждой записи
            now = time.time()
            if now - self._last_cleanup > 60:  # раз в минуту
                self._last_cleanup = now
                self._cleanup_old(now)
        except Exception:
            # Не падать если логирование сломалось
            pass

    def _cleanup_old(self, now: float) -> None:
        """Удалить записи старше retention_seconds."""
        cutoff = now - self._retention
        # deque popleft — O(1)
        while self._buffer and self._buffer[0].ts < cutoff:
            self._buffer.popleft()

    def get_recent(self, hours: float = 24.0, level: int | None = None,
                   logger_prefix: str | None = None,
                   limit: int = 500) -> list[_LogEntry]:
        """Получить записи за последние `hours` часов.

        Args:
            hours: сколько часов назад (max 24)
            level: минимальный уровень (logging.WARNING, logging.ERROR и т.д.)
                   если None — все уровни (INFO+)
            logger_prefix: фильтр по имени логгера (начинается с)
            limit: максимум записей (последние N)
        """
        cutoff = time.time() - min(hours, 24.0) * 3600
        result: list[_LogEntry] = []
        # Идём с конца — последние записи первыми
        for entry in reversed(self._buffer):
            if entry.ts < cutoff:
                break
            if level is not None and entry.level < level:
                continue
            if logger_prefix and not entry.logger_name.startswith(logger_prefix):
                continue
            result.append(entry)
            if len(result) >= limit:
                break
        result.reverse()  # в хронологическом порядке
        return result

    def get_stats(self) -> dict:
        """Статистика буфера для отладки."""
        now = time.time()
        counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        oldest_ts = now
        for entry in self._buffer:
            counts[entry.level_name] = counts.get(entry.level_name, 0) + 1
            if entry.ts < oldest_ts:
                oldest_ts = entry.ts
        return {
            "total": len(self._buffer),
            "by_level": counts,
            "oldest_age_seconds": int(now - oldest_ts) if self._buffer else 0,
            "retention_hours": self._retention / 3600,
        }


# Singleton instance — импортируется другими модулями
_memory_handler: MemoryLogHandler | None = None


def get_memory_handler() -> MemoryLogHandler:
    """Получить (или создать) singleton MemoryLogHandler."""
    global _memory_handler
    if _memory_handler is None:
        _memory_handler = MemoryLogHandler()
    return _memory_handler


def install(root_logger_name: str = "") -> MemoryLogHandler:
    """Установить handler на root logger (или на конкретный).

    Вызывается один раз при старте приложения (в main.py).
    """
    logger = logging.getLogger(root_logger_name)
    handler = get_memory_handler()
    # Не добавляем повторно
    already = any(isinstance(h, MemoryLogHandler) for h in logger.handlers)
    if not already:
        logger.addHandler(handler)
    return handler


def format_logs_for_tg(entries: list[_LogEntry], max_chars: int = 3500) -> str:
    """Сформатировать логи для отправки в TG (с лимитом длины сообщения).

    Telegram sendMessage лимит — 4096 символов. Оставляем запас под заголовок
    и обрезку. Если логи длиннее — берём последние по времени (самые свежие).
    """
    if not entries:
        return "<i>Логи пусты за указанный период.</i>"

    # Сначала пробуем вместить все
    lines = [e.format() for e in entries]
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return f"<pre>{_escape_html(full)}</pre>"

    # Не влезает — берём последние (самые свежие)
    # Идём с конца, копим пока не упрёмся в лимит
    kept: list[str] = []
    used = 0
    header = "<i>(показаны последние N записей — полный объём превышает лимит TG)</i>\n\n<pre>"
    footer = "</pre>"
    budget = max_chars - len(header) - len(footer) - 100  # запас
    for line in reversed(lines):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    return f"{header}{_escape_html(chr(10).join(kept))}{footer}"


def _escape_html(text: str) -> str:
    """Экранирование HTML для TG."""
    import html as html_mod
    return html_mod.escape(text, quote=False)
