"""Конфигурация приложения. Читает env-переменные."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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

        db_path_env = os.environ.get("DATABASE_PATH", "data/bot.db")
        db_path = Path(db_path_env)
        db_path.parent.mkdir(parents=True, exist_ok=True)

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
        )


settings = Settings.from_env()
