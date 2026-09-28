"""Дата-классы для строк БД."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Watched:
    id: int | None
    title: str
    watched_at: datetime
    rating: int | None  # 1-10 или None
    watcher_user_id: int  # кто пометил


@dataclass
class Quote:
    id: int | None
    author: str  # @упоминание или имя (как текст)
    author_user_id: int | None  # Discord ID автора, если известен
    text: str
    recorded_by: int  # Discord ID записавшего
    recorded_at: datetime


@dataclass
class MovieNight:
    id: int | None
    title: str | None  # опциональное название/описание
    scheduled_at: datetime
    created_by: int
    created_at: datetime
    event_id: int | None  # Discord Scheduled Event ID


@dataclass
class Setting:
    """Универсальная таблица настроек: key → encrypted value (для токенов) или plain."""
    key: str
    value: str | None  # для секретов — уже зашифрован, для plain — как есть
    is_secret: bool
