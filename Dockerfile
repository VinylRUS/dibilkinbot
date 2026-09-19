# Dockerfile для BotHost Pro.
#
# Стратегия: копируем ВЕСЬ build context (корень репо), но .dockerignore
# исключает data/, __pycache__/, .venv/, .env и т.п.
# Так нам не нужно беспокоиться о том, что пустая папка static/ не сохранилась
# через Git — она создаётся через mkdir -p внутри образа.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# IMPORTANT (BotHost gotcha): /app монтируется BotHost'ом из Git.
# Кладём код в /srv/app — там bind-mount его не затрёт.
WORKDIR /srv/app

COPY requirements.txt /srv/app/requirements.txt
RUN python -m venv /srv/venv \
    && /srv/venv/bin/pip install --no-cache-dir -r /srv/app/requirements.txt

# Копируем весь build context. .dockerignore исключает data/, venv и т.п.
COPY . /srv/app/

# Гарантируем существование служебных папок (на случай если Git не сохранил пустые)
RUN mkdir -p /srv/app/static /srv/app/templates /app/data

ENV DATABASE_PATH=/app/data/bot.db
ENV PYTHONUNBUFFERED=1
ENV PATH="/srv/venv/bin:$PATH"

EXPOSE 8000

CMD ["python", "main.py"]
