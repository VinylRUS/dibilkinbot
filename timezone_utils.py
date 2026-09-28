"""Утилиты для МСК времени (UTC+3)."""
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

def now_msk() -> datetime:
    """Текущее время по МСК (timezone-aware)."""
    return datetime.now(MSK)

def now_msk_iso() -> str:
    """Текущее время по МСК в ISO формате."""
    return now_msk().isoformat()

def format_msk(dt_str: str) -> str:
    """Преобразовать ISO строку в 'DD.MM.YYYY HH:MM' по МСК.
    Если строка в UTC — конвертирует в МСК."""
    if not dt_str:
        return "—"
    try:
        # Парсим ISO
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        # Если naive — считаем UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Конвертируем в МСК
        msk_dt = dt.astimezone(MSK)
        return msk_dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return dt_str[:16].replace("T", " ") if len(dt_str) > 16 else dt_str

def format_msk_short(dt_str: str) -> str:
    """Преобразовать ISO строку в 'DD.MM.YYYY' по МСК."""
    if not dt_str:
        return "—"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        msk_dt = dt.astimezone(MSK)
        return msk_dt.strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return dt_str[:10] if len(dt_str) >= 10 else dt_str
