# Kinovecher Bot

Discord-бот для уютного сервера: киновечера (через Pointauc), цитатник, кросс-постинг в Telegram, веб-панель управления.

## Стек

- **Python 3.11+**
- `discord.py` 2.x — бот + slash commands
- `FastAPI` + `uvicorn` — веб-панель
- `aiosqlite` — SQLite (персистентный на BotHost в `/app/data/bot.db`)
- `httpx` — клиент Pointauc API и Telegram Bot API
- `cryptography` (Fernet) — шифрование токенов в БД

## Архитектура

Один процесс: FastAPI (слушает `0.0.0.0:$PORT`) и discord.py клиент в одном asyncio event loop'е.
Настройки читаются из SQLite, изменения через веб-панель подхватываются в течение 30 секунд (кеш).

## Pointauc API

Используются 2 эндпоинта (остальное из официальной .NET-либы нам не нужно):

| Метод | HTTP | Назначение |
|---|---|---|
| `add_bid` | `POST /api/oshino/bids` | Добавить пункт в колесо |
| `list_lots` | `GET /api/oshino/lots` | Получить текущее колесо |

Авторизация: `Authorization: Bearer <Personal Token>` (из https://pointauc.com/settings).

## Запуск локально

```bash
cd kinovecher
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# сгенерировать APP_SECRET:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# вставить в .env, прописать DISCORD_TOKEN и т.д.

python -m app.main
```

Бот доступен в Discord, веб-панель — на http://localhost:8000

## Команды Discord

### Киновечера
- `/wheel add <название>` — добавить фильм в колесо Pointauc
- `/wheel list` — показать текущие пункты колеса
- `/watched <название> [оценка 1-10]` — пометить просмотренным, пост в TG-бэклог
- `/movienight <дата> <время> [описание]` — анонс + Discord Scheduled Event + пинг роли

### Цитатник
- `/funword @автор <текст цитаты>` — красивый embed в выделенный канал #цитатник

## Веб-панель

- `/login` — форма входа (логин/пароль из env или БД)
- `/` — дашборд
- `/tokens` — Discord / Telegram / Pointauc токены (зашифрованы Fernet)
- `/channels` — привязка каналов (#цитатник, чат-анонсов, роль пинга)
- `/features` — вкл/выкл фич (TG кросс-пост цитат/анонсов)
- `/logout`

## Деплой на BotHost Pro

1. Создать бота в дашборде BotHost, выбрать Python, подключить Git-репозиторий.
2. Включить опцию **«Использовать домен»** → получить `*.bothost.tech` поддомен.
3. В env-переменных дашборда прописать:
   - `APP_SECRET` (сгенерированный Fernet-ключ)
   - `ADMIN_LOGIN`, `ADMIN_PASSWORD`
   - `DATABASE_PATH=/app/data/bot.db`
4. BotHost сам подставит `PORT` (FastAPI слушает на нём).
5. Токены Discord/Telegram/Pointauc вводятся уже через веб-панель после первого запуска.

## Структура

```
kinovecher/
├── app/
│   ├── main.py          # точка входа: uvicorn + discord.py
│   ├── config.py        # настройки из env
│   ├── db.py            # aiosqlite + миграции
│   ├── models.py        # дата-классы для строк
│   ├── crypto.py        # Fernet-шифрование токенов
│   ├── pointauc.py      # HTTP-клиент Pointauc (2 эндпоинта)
│   ├── telegram.py      # TG sendMessage
│   ├── bot.py           # discord.py Bot + slash commands
│   ├── web.py            # FastAPI + Jinja2
│   └── templates/
│       ├── login.html
│       ├── panel.html
│       └── ...
├── data/                # bot.db здесь
├── requirements.txt
├── Dockerfile
└── .env.example
```

## TODO после MVP

- `/wheel sync` — синхрон состояния колеса из Pointauc (уже есть list_lots, осталась логика)
- Оценки + годовая статистика
- «Цитата дня»
- `/remind <время> <текст>`
- Игровечера: `/game <ссылка>` с embed + сбор участников реакцией
