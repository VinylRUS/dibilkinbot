# DeeBeelkin Bot

Discord-бот для уютного сервера: киновечера с колесом, цитатник, кросс-постинг в Telegram, веб-панель управления.

## Стек

- **Python 3.11+**, asyncio
- `discord.py` 2.x — бот + slash commands
- `FastAPI` + `uvicorn` — веб-панель (один процесс с ботом)
- `aiosqlite` — SQLite (персистентный на BotHost в `/app/data/bot.db`)
- `httpx` — клиент Telegram Bot API
- `cryptography` (Fernet) — шифрование токенов в БД
- `kinopoiskapiunofficial.tech` — поиск фильмов, постеры, описания
- WebSocket — real-time обновления колеса в браузере

## Архитектура

Один процесс: FastAPI слушает `0.0.0.0:$PORT`, discord.py клиент живёт в том же asyncio event loop'е.
Настройки в SQLite, изменения через веб-панель подхватываются в течение 30 секунд (кеш).

Multi-tenant: каждый Discord-сервер изолирован по `guild_id` — свои цитаты, победители, бэклог, списки желаемого, каналы, фичи.

## Команды Discord

### Киновечера и списки
- `/addfilm <название>` — добавить фильм в личный список желаемого (ephemeral, виден только вам)
- `/delfilm <название|#id>` — удалить фильм из списка (ephemeral)
- `/myfilms` — посмотреть свой список желаемого (ephemeral)
- `/movienight <дата> <время> [описание]` — анонс киновечера + Discord Scheduled Event + пинг роли
- `/watched <название> [оценка 1-10]` — пометить просмотренным, пост в TG-бэклог

### Цитатник
- `/funword @автор <текст цитаты>` — красивый embed в выделенный канал #цитатник
- `/setquoteemoji <эмодзи>` — смена эмодзи для авто-захвата цитат
- Авто-захват: поставь настроенный эмодзи на любое сообщение — бот сохранит как цитату

### Прочее
- `/linktg` — привязка Telegram-аккаунта (для личных уведомлений)
- `/changelog` — показать текущую версию и что нового

## Веб-панель

| Страница | Назначение |
|---|---|
| `/login` | Вход по Discord ID (или логин/пароль для админа) |
| `/` | Дашборд: статус бота, пинги Discord/TG, лента активности, последние победители и бэклог, топ авторов цитат |
| `/wheel` | Колесо Pointauc: вращение, режимы (классический / на выбывание), кнопка «Загрузить из списков желаемого» |
| `/winners` | История победителей с подтверждением/оценками |
| `/watched` | Бэклог с пагинацией по 20 фильмов, редактированием названия и оценкой 1–10 |
| `/quotes` | Цитатник: импорт из канала, экспорт в TXT |
| `/profile` | Личные данные + список желаемого с добавлением/редактированием/удалением |

**Админ-only** (видны только в выпадающем меню «Бот»):
- `/tokens` — Discord / Telegram / Pointauc токены (зашифрованы Fernet)
- `/channels` — привязка каналов (#цитатник, чат-анонсов, роль пинга)
- `/features` — вкл/выкл фич (TG кросс-пост цитат/анонсов/победителей)
- `/users` — управление юзерами
- `/guilds` — управление серверами

Тёмная/светлая тема — кнопка 🌙/☀️ в навбаре. Время везде по МСК (UTC+3).

## Деплой на BotHost Pro

1. Создать бота в дашборде BotHost, выбрать Python, подключить Git-репозиторий.
2. Включить опцию **«Использовать домен»** → получить `*.bothost.tech` поддомен.
3. В env-переменных дашборда прописать:
   - `APP_SECRET` (сгенерированный Fernet-ключ: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)
   - `ADMIN_LOGIN`, `ADMIN_PASSWORD`
   - `ADMIN_DISCORD_ID` — Discord ID главного админа
   - `DATABASE_PATH=/app/data/bot.db`
4. BotHost сам подставит `PORT` (FastAPI слушает на нём).
5. Токены Discord/Telegram/Pointauc вводятся уже через веб-панель после первого запуска.

Деплой: git push → BotHost подтягивает. SQLite в `/app/data/` переживает редеплой.

## Структура

```
kinovecher/
├── main.py                 # точка входа: uvicorn + discord.py
├── config.py               # настройки из env
├── db.py                   # aiosqlite + миграции (multi-tenant)
├── models.py               # дата-классы для строк
├── crypto.py               # Fernet-шифрование токенов
├── kinopoisk.py            # клиент kinopoiskapiunofficial.tech
├── telegram.py             # TG Bot API (sendMessage, sendPhoto, createForumTopic)
├── bot.py                  # discord.py Bot + slash commands + on_message/reaction
├── web.py                  # FastAPI + Jinja2 (панель + API)
├── guild.py                # управление guild-таблицами
├── ws_manager.py           # WebSocket для real-time колеса
├── changelog_parser.py     # парсер CHANGELOG.md
├── timezone_utils.py       # MSK (UTC+3) форматирование
├── templates/              # HTML: login, panel, _nav, channels, tokens, features,
│                           #         users, guilds, quotes, wheel, winners,
│                           #         watched, profile, select_guild
├── static/                 # style.css, bee.png
├── requirements.txt
├── Dockerfile              # uv-based для быстрого деплоя
└── .env.example
```

## Discord Intents (нужны в Developer Portal)

- Server Members Intent: ON
- Message Content Intent: ON
- (Privileged, включаются в Bot settings)

## TODO

- Ачивки за просмотры/оценки/цитаты
- Годовая/месячная статистика по оценкам
- «Цитата дня» — случайная цитата в заданное время
- Напоминания: `/remind <время> <текст>`
- Игровечера: `/game <ссылка>` с embed + сбор участников реакцией
