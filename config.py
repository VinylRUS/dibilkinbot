"""Конфигурация приложения. Читает env-переменные."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Хардкод персистентной папки BotHost. Согласно документации:
# https://bothost.ru/docs/database-storage.md
# https://bothost.ru/docs/common-errors.md
# Только /app/data переживает git-push / пересборку образа. Никаких других путей.
PERSISTENT_DIR = Path("/app/data")


@dataclass(frozen=True)
class Settings:
    # Web server
    port: int
    host: str

    # Auth
    app_secret: str  # Fernet key
    admin_login: str
    admin_password: str

    # DB
    database_path: Path

    # Discord
    discord_token: str | None  # может прийти через панель, тогда env пустой

    # Telegram
    telegram_token: str | None
    telegram_chat_id: str | None

    # Pointauc
    pointauc_token: str | None

    # Kinopoisk (kinopoisk.dev)
    kinopoisk_token: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        port = int(os.environ.get("PORT", "8000"))
        host = os.environ.get("HOST", "0.0.0.0")

        app_secret = os.environ.get("APP_SECRET", "").strip()
        if not app_secret:
            raise RuntimeError(
                "APP_SECRET обязателен. Сгенерируйте: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )

        # === Путь к БД ===
        # Дефолт — хардкод /app/data/bot.db (BotHost persistence).
        # Если env DATABASE_PATH задан — используем его, но с предупреждением если не в /app/data.
        env_db = os.environ.get("DATABASE_PATH")
        if env_db:
            db_path = Path(env_db)
            if not str(db_path).startswith("/app/data"):
                print(
                    f"[config] WARNING: DATABASE_PATH={env_db} — это не в /app/data. "
                    f"БД НЕ будет персистентной между деплоями! "
                    f"Рекомендую: DATABASE_PATH=/app/data/bot.db",
                    file=sys.stderr,
                )
        else:
            db_path = PERSISTENT_DIR / "bot.db"

        # Создаём директорию и проверяем запись — без молчаливого fallback.
        # Если не вышло — падаем с понятной ошибкой (лучше краш, чем тихая потеря данных).
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            raise RuntimeError(
                f"НЕВОЗМОЖНО СОЗДАТЬ ПАПКУ БД: {db_path.parent} ({e}). "
                f"Проверьте права доступа в Dockerfile: RUN mkdir -p /app/data && chmod 777 /app/data"
            ) from e

        # Тест записи — создаём пустой файл
        try:
            db_path.touch(exist_ok=True)
        except (OSError, PermissionError) as e:
            raise RuntimeError(
                f"НЕВОЗМОЖНО ЗАПИСАТЬ БД: {db_path} ({e}). "
                f"Контейнер должен иметь права на запись в /app/data. "
                f"В Dockerfile должно быть: RUN chmod 777 /app/data"
            ) from e

        return cls(
            port=port,
            host=host,
            app_secret=app_secret,
            admin_login=os.environ.get("ADMIN_LOGIN", "admin"),
            admin_password=os.environ.get("ADMIN_PASSWORD", "changeme"),
            database_path=db_path,
            discord_token=os.environ.get("DISCORD_TOKEN") or None,
            telegram_token=os.environ.get("TELEGRAM_TOKEN") or None,
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
            pointauc_token=os.environ.get("POINTAUC_TOKEN") or None,
            kinopoisk_token=os.environ.get("KINOPOISK_TOKEN") or None,
        )


settings = Settings.from_env()
