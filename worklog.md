# DeeBeelkin Bot — Worklog

## Описание проекта
DeeBeelkin — Discord-бот для уютного сервера (киновечера + цитатник). 
Стек: Python 3.12, FastAPI (веб-панель), discord.py 2.x, SQLite (aiosqlite), uv (Dockerfile).
Хостинг: BotHost Pro (https://dibilkis.bothost.tech/)
GitHub: https://github.com/VinylRUS/dibilkinbot

## Важные напоминания
- **GitHub PAT токен** хранится в переменных окружения или передаётся в чате. НЕ коммитить. НЕ пушить напрямую в main — только через PR (feature-ветка → PR → merge).
- **CHANGELOG.md** лежит в корне проекта. Правила:
  - `### Новое` — попадает в Discord-анонс (видимый всем функционал)
  - `### Улучшено` — попадает в анон (UI/UX изменения)
  - `### Техническое` — НЕ попадает в анон (багфиксы бэкенда, API-изменения, админское)
- **BotHost**: bind-mount на /app, данные в /app/data/ (персистентные). CHANGELOG.md ищется по путям: CWD, /app/, рядом с bot.py.
- **Деплой**: через GitHub (git push → BotHost подтягивает) или через zip-архив в download/.
- **Archives**: НЕ включать worklog.md в архивы. Это файл для контекста сессии.

## Текущая версия: v1.6.5

## Архитектура
- `bot.py` — Discord бот (slash commands, on_message, on_raw_reaction_add, on_guild_join/remove)
- `web.py` — FastAPI веб-панель (логин, дашборд, tokens, channels, features, users, guilds, quotes, wheel, winners, watched, profile)
- `db.py` — SQLite с multi-tenant (g_* функции принимают guild_id). Старые функции = aliases к guild_id=0.
- `guild.py` — управление guild-таблицами (init, drop, upsert, approve, migrate)
- `kinopoisk.py` — клиент kinopoiskapiunofficial.tech (X-API-KEY header)
- `telegram.py` — TG Bot API (sendMessage, sendPhoto, createForumTopic)
- `ws_manager.py` — WebSocket для real-time обновлений колеса
- `changelog_parser.py` — парсер CHANGELOG.md
- `config.py` — env-переменные (APP_SECRET, ADMIN_DISCORD_ID, ADMIN_PASSWORD, DATABASE_PATH, токены)
- `templates/` — Jinja2 HTML (login, panel, _nav, channels, tokens, features, users, guilds, quotes, wheel, winners, watched, profile, select_guild)
- `static/style.css` — общий CSS (тёмная/светлая тема, dropdown, анимации, ping-карточки)

## Discord Intents (нужны в Developer Portal)
- Server Members Intent: ON
- Message Content Intent: ON
- (Privileged, включаются в Bot settings)

## Команды Discord
- `/wheel add/list/remove` — управление колесом
- `/funword @author text` — ручная цитата
- `/movienight date time [description]` — анонс киновечера
- `/filmnight start/end/status [max_per_user]` — сбор фильмов
- `/setquoteemoji emoji` — смена эмодзи для захвата цитат
- `/changelog` — показать версию и что нового
- `/linktg` — привязка Telegram

## Планы на v1.7.0
1. Бэклог: редактирование названия фильма, оценка для вручную добавленных
2. Колесо: переключатель режима (выбывание/обычный), настройка времени вращения
3. Фикс навбара: кнопки Комьюнити/Бот/тема сползли вниз относительно Профиль/Выйти
4. Бэклог: курсор в поле ввода после добавления фильма
