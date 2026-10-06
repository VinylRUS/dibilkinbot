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

## Планы на v1.7.1
1. Убрать анимации fadeInUp с больших панелей (бэклог и др.)
2. Пагинация бэклога (20 фильмов, кнопка "показать ещё")
3. Список желаемого:
   - Личный у каждого юзера (guild_{id}_watchlist таблица)
   - Discord: /addfilm (ephemeral), /delfilm (ephemeral), /myfilms (ephemeral)
   - Веб-панель: на странице /profile — список фильмов с кнопками ✏️ и ×
   - Победитель колеса помечается как "просмотрен" в списке (не удаляется)
4. Колесо:
   - Убрать /filmnight (start/end/status) и всю логику сбора
   - Убрать /wheel add (заменяется списком желаемого)
   - На странице /wheel — кнопка "Загрузить из списков желаемого" (все непросмотренные фильмы всех юзеров)
   - /wheel list и /wheel remove остаются
5. Время по МСК (UTC+3) — хардкод, отображение в панели и Discord

---
Task ID: review-v1.8.4
Agent: main
Task: Полное код-ревью проекта (db.py, web.py, bot.py, guild.py, config.py, main.py, ws_manager.py, crypto.py, templates/, style.css)

Work Log:
- Запустил 4 параллельных Explore-агента для ревью разных частей кодовой базы
- Каждый агент вернул детальный отчёт с номерами строк, severity и предложениями фиксов
- Сформировал сводный отчёт в /home/z/my-project/kinovecher/CODEREVIEW.md

Stage Summary:
- Всего найдено 121 проблема: 18 CRITICAL, 27 HIGH, 40 MEDIUM, 36 LOW
- Главные критичные проблемы:
  1. Multi-tenant изоляция сломана (is_guild_member не проверяет конкретный guild)
  2. is_admin глобальный, не per-guild, кешируется в cookie на 7 дней
  3. WebSocket /ws/wheel без auth + утечка added_by через broadcast
  4. Пароли: 1000 iter SHA-256 без HMAC, мин. длина 4
  5. /login без rate-limiting (brute-force)
  6. Race conditions в g_get_or_create_winner_by_title, g_complete_collection_spin, g_start_collection, verify_tg_link
  7. 15 POST-форм без CSRF-токенов
  8. XSS в onsubmit="confirm('...{{ user_var }}...')" (4 шаблона)
  9. notify_wheel_winner хардкодит guild_id=0
  10. config.py: дефолт пароля changeme + path traversal в DATABASE_PATH
- Что хорошо: SQL-инъекций через имена таблиц нет, async with везде корректен, Jinja2 autoescape включён, Fernet для секретов
- План: 3 фазы исправления (безопасность → race/целостность → производительность/UX)

---
Task ID: roadmap-v2.0
Agent: main
Task: План доработок и мелких улучшений (обсуждение с пользователем)

Work Log:
- Пользователь показал скриншот sidebar footer — аватарка смещена влево,
  кнопки (профиль, тема, выход) по центру. Выглядит несбалансированно.
- Записано в планы: выровнять аватарку по центру в свёрнутом sidebar
- Пользователь предложил 4 пункта плана (см. Stage Summary)
- Запрошены идеи по мелким улучшениям

Stage Summary:
ПЛАН ДОРАБОТОК (согласованный):

От пользователя:
1. Редактирование текста ачивок (название/описание) после создания
2. Ретроспективная выдача ачивок (проверять при создании ачивки —
   все кто уже выполнил условия, получить автоматически)
3. Иконки ачивок из URL загружать в static/icons/ автоматически
4. Хранение предложений от юзеров (придумать реализацию позже)

От меня (мелкие улучшения):
5. Sidebar footer — выровнять аватарку по центру (свёрнутый режим)
6. Бейдж «🆕 Только что» в бэклоге — для фильмов добавленных за 7 дней
7. Свои достижения в sidebar — мини-бейдж последней ачивки рядом с
   аватаром
8. Сортировка бэклога — добавить опцию «по оценке» (топ фильмов)
9. Полнотекстовый поиск по описаниям ачивок
10. Спиннер вместо '...' при отправке форм
11. Авто-выбор тёмной темы на основе prefers-color-scheme
12. Discord виджет на дашборде — кто онлайн в voice channel
13. Уведомление в TG когда кто-то оценил фильм который ты тоже оценил
14. Кнопка «Предложить фильм» — юзер предлагает фильм, попадает в
    общий список на голосование
15. Статистика по жанрам — «в этом году: 12 sci-fi, 8 драм»
16. Годовой recap — «2026 вместе: 47 фильмов» в конце года
17. Отзывы (текст, не только звёзды) — после просмотра каждый пишет
    короткий отзыв
18. Pin любимого фильма — 3 любимых фильма в профиле

Приоритет: пункты 1-3 сделать в первую очередь (полировка ачивок),
пункт 4 и 17 — фичи для «сладкой начинки», остальное по мере.

---
Task ID: roadmap-v2.0-update
Agent: main
Task: Обновление плана после обсуждения с пользователем

Work Log:
- Пользователь одобрил/отклонил пункты плана
- Запрошено обсуждение пункта 11 (Discord данные) подробнее

Stage Summary:
ОДОБРЕННЫЕ ПУНКТЫ:
5. Sidebar footer — выровнять аватарку по центру
8. Сортировка бэклога — опция «по оценке»
9. Спиннер вместо '...' при отправке форм (красивый, не багованный)
1. Редактирование текста ачивок
2. Ретроспективная выдача ачивок
3. Иконки ачивок из URL → static/icons/
4. Хранение предложений от юзеров (позже)
16. Отзывы (текст, не обязательные)

ОТКЛОНЁННЫЕ:
6. Бейдж «🆕» — не нужно
7. Мини-бейдж ачивки в sidebar — не нужно
10. Auto theme — не нужно, тёмная по умолчанию
12. TG уведомление об оценках — спам
13. Кнопка «Предложить фильм» — лишняя
14. Статистика по жанрам — лишние запросы к API
15. Годовой recap — пока не надо
17. Pin любимого фильма — не нужна

ПЕРЕИНАЧЕННЫЙ ПУНКТ 11:
- НЕ Discord виджет «кто онлайн в voice»
- ВМЕСТО: брать больше информации из Discord о юзере:
  сколько времени провёл в войсе, что делал и прочее
- Вопрос: как достать эту инфу? Нужны вебхуки?
- Обсуждение в процессе
