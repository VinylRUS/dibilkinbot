# Dockerfile для BotHost Pro — оптимизированный с uv.
# uv в 10-100x быстрее pip для установки пакетов.

FROM python:3.12-slim

# Минимальные системные пакеты (build-essential для cryptography)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev git \
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

# v2.1.4: Записываем commit hash в .git_commit для футера версии.
# Приоритет: --build-arg GIT_COMMIT > git rev-parse > дата сборки.
ARG GIT_COMMIT=auto
RUN if [ "$GIT_COMMIT" = "auto" ] || [ -z "$GIT_COMMIT" ]; then \
      COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo ""); \
      if [ -z "$COMMIT" ]; then \
        COMMIT=$(date +%Y%m%d); \
      fi; \
    else \
      COMMIT="$GIT_COMMIT"; \
    fi; \
    echo "$COMMIT" > /app/.git_commit && echo "Git commit: $COMMIT"

ENV DATABASE_PATH=/app/data/bot.db
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8000
CMD ["/opt/venv/bin/python", "main.py"]
