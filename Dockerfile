# Dockerfile для BotHost Pro.
# Согласно документации BotHost (https://bothost.ru/docs/custom-dockerfile.md,
# https://bothost.ru/docs/database-storage.md), Python совместим с bind-mount на /app
# без изменений — исходники запускаются напрямую.
#
# Структура при работе на BotHost:
#   /app/           ← BotHost bind-mount'ит сюда Git source при старте контейнера
#   /app/data/      ← персистентный volume, BotHost НЕ затирает при git-push
#   /app/data/bot.db ← SQLite, переживает редеплой
#
# Локальная разработка: образ просто использует код из build context.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем зависимости (это не зависит от bind-mount — venv в /opt)
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Создаём персистентную папку BotHost (если ещё не создана) с правами на запись
RUN mkdir -p /app/data && chmod 777 /app/data

# Копируем код. На BotHost это перетрётся bind-mount'ом Git source — это нормально.
# Локально (без bind-mount) код будет жить в /app/.
WORKDIR /app
COPY . /app/

ENV DATABASE_PATH=/app/data/bot.db
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8000

CMD ["python", "main.py"]
