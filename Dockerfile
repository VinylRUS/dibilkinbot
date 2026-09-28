# Dockerfile для BotHost Pro — оптимизированный с uv.
# uv в 10-100x быстрее pip для установки пакетов.

FROM python:3.12-slim

# Минимальные системные пакеты (build-essential для cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем uv (быстрый пакетный менеджер)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# === СЛОЙ ЗАВИСИМОСТЕЙ (кешится — не пересобирается при git-push) ===
COPY requirements.txt /tmp/requirements.txt
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# === СЛОЙ КОДА (пересобирается при git-push) ===
RUN mkdir -p /app/data && chmod 777 /app/data
WORKDIR /app
COPY . /app/

ENV DATABASE_PATH=/app/data/bot.db
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8000
CMD ["/opt/venv/bin/python", "main.py"]
