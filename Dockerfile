# Multi-stage build для Python на BotHost
# BotHost нативно поддерживает Python через requirements.txt, но мы даём
# явный Dockerfile чтобы зафиксировать версию Python и структуру.

FROM python:3.11-slim

# Системные зависимости для сборки cryptography и sqlite3 (в образе уже есть)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# IMPORTANT (BotHost gotcha):
# каталог /app при запуске контейнера монтируется из Git. Поэтому бинарники и venv
# кладём в /srv, а данные — в /app/data (который персистентен и исключён из Git).

WORKDIR /srv/app

COPY requirements.txt /srv/app/requirements.txt
RUN python -m venv /srv/venv \
    && /srv/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app /srv/app/app

# Каталог данных — отдельный, BotHost его не затирает при git-push
RUN mkdir -p /app/data

ENV DATABASE_PATH=/app/data/bot.db
ENV PYTHONUNBUFFERED=1
ENV PATH="/srv/venv/bin:$PATH"

EXPOSE 8000

CMD ["python", "-m", "app.main"]
