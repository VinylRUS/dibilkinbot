# Dockerfile для BotHost Pro. Структура архива плоская (все .py в корне).
#
# BotHost разворачивает Git-репо в /app. Папка /app/data монтируется BotHost'ом
# как персистентный volume — туда кладём SQLite. ВАЖНО: не копируем data/ из
# build context в образ, иначе при git-push данные затрутся.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# IMPORTANT (BotHost gotcha): /app монтируется из Git. Бинарники и venv кладём
# в /srv — они не должны перетираться bind-mount'ом.
WORKDIR /srv/app

COPY requirements.txt /srv/app/requirements.txt
RUN python -m venv /srv/venv \
    && /srv/venv/bin/pip install --no-cache-dir -r /srv/app/requirements.txt

# Копируем только код и шаблоны — НЕ data/ (она персистентная на BotHost)
COPY *.py /srv/app/
COPY templates /srv/app/templates
COPY static /srv/app/static

# Создаём /app/data на случай если BotHost не примонтировал volume
RUN mkdir -p /app/data

ENV DATABASE_PATH=/app/data/bot.db
ENV PYTHONUNBUFFERED=1
ENV PATH="/srv/venv/bin:$PATH"

EXPOSE 8000

CMD ["python", "main.py"]
