"""v2.3.3: Логирование тестового режима колеса.

Каждый тест записывается в /app/data/wheel_test_logs/test_YYYYMMDD_HHMMSS_<admin_id>.log
В лог попадают:
- Start/end теста (кто, когда, guild_id)
- Каждый спин (mode, items before/after, winner/eliminated)
- Постинг в Discord (channel, message_id, success)
- Постинг в TG (chat, success)
- Уведомления юзерам (кому, success)
- Cleanup статистика

Лог-файлы персистентны — переживают redeploy, доступны для последующего анализа.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("wheel_test")

# Папка для логов — персистентная, в /app/data
_LOG_DIR = Path(os.environ.get("WHEEL_TEST_LOG_DIR", "/app/data/wheel_test_logs"))
# Fallback для локальной разработки
if not _LOG_DIR.exists():
    _LOG_DIR = Path("./wheel_test_logs")

# Текущий открытый лог-файл (один тест за раз)
_current_log_path: Path | None = None
_current_log_fp = None


def _ensure_dir() -> None:
    """Создать папку для логов если её нет."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log.warning("Cannot create wheel test log dir %s: %s", _LOG_DIR, e)


def start_test_log(admin_discord_id: int, admin_username: str, guild_id: int) -> str:
    """Начать новый лог-файл для теста.

    Возвращает абсолютный путь к созданному файлу.
    Если уже есть открытый лог — закрывает его (не должно случаться при нормальном flow).
    """
    global _current_log_path, _current_log_fp
    if _current_log_fp is not None:
        try:
            _current_log_fp.close()
        except Exception:
            pass
    _ensure_dir()
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"test_{ts}_{admin_discord_id}.log"
    _current_log_path = _LOG_DIR / filename
    _current_log_fp = open(_current_log_path, "a", encoding="utf-8")
    _write("=== TEST SESSION START ===")
    _write(f"timestamp: {datetime.utcnow().isoformat()}")
    _write(f"admin_discord_id: {admin_discord_id}")
    _write(f"admin_username: {admin_username}")
    _write(f"guild_id: {guild_id}")
    _write(f"log_file: {_current_log_path}")
    _write("---")
    log.info("Wheel test log started: %s", _current_log_path)
    return str(_current_log_path)


def log_event(event_type: str, payload: dict[str, Any]) -> None:
    """Записать событие в текущий лог.

    event_type: 'spin', 'elimination', 'discord_post', 'tg_post', 'tg_notify',
                'discord_notify', 'error', 'cleanup', 'info'
    payload: dict с деталями события.
    """
    if _current_log_fp is None:
        return
    _write(f"[{event_type}] {json.dumps(payload, ensure_ascii=False, default=str)}")


def end_test_log(cleanup_stats: dict | None = None) -> str | None:
    """Завершить текущий лог. Возвращает путь к файлу."""
    global _current_log_path, _current_log_fp
    if _current_log_fp is None:
        return None
    _write("---")
    _write("=== TEST SESSION END ===")
    _write(f"timestamp: {datetime.utcnow().isoformat()}")
    if cleanup_stats:
        _write(f"cleanup_stats: {json.dumps(cleanup_stats, ensure_ascii=False, default=str)}")
    _write("")
    path = str(_current_log_path)
    try:
        _current_log_fp.close()
    except Exception:
        pass
    _current_log_fp = None
    _current_log_path = None
    log.info("Wheel test log closed: %s", path)
    return path


def get_current_log_path() -> str | None:
    """Вернуть путь к текущему открытому логу (или None если тест не идёт)."""
    return str(_current_log_path) if _current_log_path else None


def list_log_files() -> list[dict]:
    """Список всех лог-файлов для UI (показать историю тестов)."""
    _ensure_dir()
    files = []
    for p in sorted(_LOG_DIR.glob("test_*.log"), reverse=True):
        try:
            stat = p.stat()
            files.append({
                "filename": p.name,
                "path": str(p),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except Exception:
            continue
    return files


def read_log_file(filename: str, max_lines: int = 500) -> str | None:
    """Прочитать содержимое лог-файла (для отображения в UI)."""
    # Безопасность: только basename, без path traversal
    safe_name = Path(filename).name
    if not safe_name.startswith("test_") or not safe_name.endswith(".log"):
        return None
    path = _LOG_DIR / safe_name
    if not path.exists() or not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > max_lines:
            return "".join(lines[:max_lines]) + f"\n... [truncated, {len(lines) - max_lines} more lines]"
        return "".join(lines)
    except Exception as e:
        log.warning("Failed to read log file %s: %s", path, e)
        return None


def _write(line: str) -> None:
    """Записать строку в текущий лог-файл."""
    if _current_log_fp is None:
        return
    try:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _current_log_fp.write(f"{ts} {line}\n")
        _current_log_fp.flush()
    except Exception as e:
        log.warning("Failed to write to wheel test log: %s", e)
