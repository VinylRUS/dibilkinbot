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

---
Task ID: feature/auto-roles-and-dedupe-games
Agent: main
Task: Сделать подгрузку ролей в БД автоматической (как и списка игр) + дедупликация игр

Work Log:
- Создана ветка feature/auto-roles-and-dedupe-games из main
- guild.py: добавлена таблица guild_{id}_discord_roles (role_id PK, name, color, position, hoisted, mentionable, permissions, synced_at). Добавлен base "discord_roles" в GUILD_TABLES
- db.py: новые функции:
  * g_sync_discord_roles(guild_id, roles) — INSERT OR REPLACE + удаление stale ролей
  * g_list_discord_roles(guild_id) — чтение кеша из БД
  * g_dedupe_games(guild_id) — для каждого (user, canonical_name) с >1 записью: DELETE всех строк + INSERT одной канонической с суммарной длительностью
  * _normalize_game_name(name) — strip + collapse whitespace (Python)
  * _NORM_GAME_SQL — SQL-выражение для нормализации (TRIM + LOWER + 5 итераций REPLACE('  ', ' '))
  * g_list_server_games переписана: GROUP BY нормализованного имени, MAX(activity_name) для display
  * g_get_game_play_time_specific теперь сравнивает по нормализованному имени
- bot.py:
  * fetch_guild_roles теперь возвращает permissions.value
  * sync_guild_roles_to_db(guild_id) — обёртка над fetch + g_sync_discord_roles
  * on_ready: для каждого guild после init_guild_tables вызывает sync_guild_roles_to_db
  * on_guild_join: тоже вызывает sync_guild_roles_to_db
  * Новые обработчики: on_guild_role_create / on_guild_role_update / on_guild_role_delete — пересинхронизируют кеш ролей
  * _handle_presence_change: нормализует имена игр через db._normalize_game_name перед сохранением
- web.py:
  * /api/achievements/discord_roles теперь читает из БД-кеша (мгновенно)
  * Новый POST /api/achievements/discord_roles/refresh — принудительная синхронизация с Discord + дедупликация игр
- templates/achievements.html:
  * loadDiscordRoles() вызывается автоматически при открытии страницы
  * Кнопка «🔄 Обновить роли» дёргает refreshDiscordRoles() (POST /refresh)
  * Кнопка игр переименована в «🔄 Обновить список игр» для консистентности
- CHANGELOG.md: добавлена запись v2.0.2
- Тесты: /home/z/my-project/scripts/test_roles_and_dedupe.py — 5 тестов покрывают sync/list roles, _normalize_game_name, g_list_server_games dedupe, g_dedupe_games, g_get_game_play_time_specific. Все 5 тестов проходят

Stage Summary:
- Роли теперь автоматически синхронизируются с БД при старте бота и при любых изменениях (create/update/delete). Веб-панель читает из кеша — мгновенно
- Дедупликация игр на двух уровнях:
  1. На вводе (_handle_presence_change нормализует имя)
  2. На чтении (g_list_server_games группирует по нормализованному имени)
  3. Опционально через кнопку refresh — g_dedupe_games схлопнет существующие дубликаты
- Совместимо со старыми данными: ничего не сломано, всё работает на свежих и существующих БД
- Готово к коммиту и PR в main

---
Task ID: feature/v2.0.3-notif-icons-games
Agent: main
Task: v2.0.3 — оповещения о новых достижениях, смена иконки ачивок, последние игры + совместимость по играм

Work Log:
- Стянул с main последнюю версию (включает v2.0.2)
- Создана ветка feature/v2.0.3-notif-icons-games из обновлённого main
- Пункт 2 (легко): sidebar.html — 'achievements': 'trophy' → 'medal', эмодзи 🏆 → 🏅 в fallback
- Пункт 3a (db.py): добавлены функции:
  * g_get_user_recent_games(guild_id, user_id, limit=5) — последние игры по ended_at DESC
  * g_get_game_compat(guild_id, user_a, user_b) — common / max(unique_a, unique_b) * 100
- Пункт 3c (web.py): /u/{discord_id} — добавлены recent_games + game_compat в контекст
- Пункт 3b (profile_public.html):
  * taste-match-card уменьшен (padding 18→10, percent 2.2→1.5rem, sub 0.85→0.78rem)
  * Убрана строка «Вместе посмотрели: N» + ветка elif watched_together (лишняя инфа)
  * Добавлен .compat-card-mini для game_compat (компактная плашка, до 3 названий игр в tooltip)
  * Добавлен блок «🎮 Последние игры» — список <ul> с game-name, duration, date
- Пункт 1a (db.py):
  * Миграция users — добавлена колонка last_viewed_achievements_at TEXT
  * g_count_unread_achievements(guild_id, user_id) — COUNT WHERE granted_at > last_viewed_at
  * g_mark_achievements_viewed(user_id) — UPDATE last_viewed_achievements_at = now
- Пункт 1b (web.py):
  * get_current_user обогащается полем unread_achievements_count (один SELECT COUNT на запрос)
  * /api/winners/{id}/rate — возврат g_check_and_grant_auto попадает в new_achievements
  * /api/watched/{id}/rate — то же самое
  * /api/quotes — то же самое (для записавшего)
  * Новый POST /api/achievements/mark_read — сброс бейджа
  * /u/{discord_id} — если юзер смотрит свой профиль, автоматически mark_achievements_viewed
- Пункт 1c (sidebar.html + style.css):
  * Bell icon в sidebar-header: классы .sb-bell / .sb-bell-empty / .sb-bell-badge
  * Бейдж с пульсацией (sb-bell-pulse keyframes) + тряска иконки (sb-bell-ring)
  * JS: markAchievementsRead(link) — POST /api/achievements/mark_read + переход
  * JS: notifyNewAchievements(list) — показывает toast «🏆 Новая ачивка: %s» с stagger
- winners.html / watched.html / quotes.html: вызов notifyNewAchievements если data.new_achievements
- Тесты: /home/z/my-project/scripts/test_v203.py — 4 теста (unread, recent_games, game_compat, dedup), все проходят

Stage Summary:
- Bell icon показывает счётчик непросмотренных ачивок. При оценке фильма / записи цитаты — toast о новой ачивке
- Иконка ачивок в сайдбаре теперь 🏅 (medal), а не 🏆 (trophy) — отличается от победителей колеса
- В профиле появился блок «🎮 Последние игры» (5 последних сыгранных, с временем) + компактная плашка совместимости по играм
- Плашка совместимости фильмов уменьшена — убран лишний текст, уменьшены размеры

---
Task ID: feature/v2.1.0-achievements-steam
Agent: main
Task: v2.1.0 — большой багофикс ачивок + liquid glass иконки + Steam игры

Work Log:
- 4 параллельных Explore-агента провели аудит:
  * Ачивки: 65 багов (4 CRITICAL, 9 HIGH, 28 MEDIUM, 24 LOW)
  * Иконки: плоский solid-fill диск, хардкод dark hex, мёртвый .ach-pulse класс
  * Steam: только 3 endpoint'а реализовано (ResolveVanityURL, GetPlayerSummaries, wishlist-проверка)
  * Трекинг активностей: presence-трекер пишет только activity_name

- Фаза 1: багофикс ачивок (8 багов)
  * L19 (HIGH): фронт умножал threshold на 3600 для ВСЕХ триггеров (даже count-триггеров).
    Ачивка "10 оценок" сохранялась как порог 36000 → никогда не срабатывала.
    Фикс: конвертация в секунды только для time-триггеров (voice_time*, game_play_time)
  * C4 (CRITICAL): авто-выдача не выдавала Discord-роль (bot_obj мёртвый параметр).
    Фикс: try/except с импортом bot внутри g_check_and_grant_auto, вызывается всегда
  * C2 (CRITICAL): ретро-выдача висла 5+ минут через list_users() по всем гильдиям.
    Фикс: _retro_grant_achievement() в asyncio.create_task, list_guild_user_ids() —
    только активные юзеры текущей гильдии
  * H1 (HIGH): повторная выдача перезаписывала granted_at → счётчик непрочитанных горел.
    Фикс: UPDATE с AND is_active = 0 (реактивируем только отозванные)
  * C1 (CRITICAL): watched_count считал всю гильдию, выдавался всем.
    Фикс: WHERE watcher_user_id = ?
  * M3: отрицательный threshold раздавал ачивку всем.
    Фикс: ValueError if threshold < 0
  * M20-22: admin не видел роли в конструкторе (require_superuser).
    Фикс: require_admin для /api/achievements/discord_roles и /refresh
  * H4/H5 (HIGH): XSS через icon_url + SSRF + custom_*.png терялись при redeploy.
    Фикс: валидация scheme (http/https), magic bytes check, /app/data/icons/ персистентный
    путь, fallback-роут /static/icons/custom/{filename}
  * Тесты: /home/z/my-project/scripts/test_v210_phase1.py — 6 тестов прошли

- Фаза 2: иконки в liquid glass стиле
  * renderAchievementSVG переписан: возвращает <span class="ach-icon-wrap"> с
    backdrop-filter + glass-bg + accent ring через currentColor
  * ACHIEVEMENT_COLORS теперь хранит CSS-переменные (var(--accent), var(--red))
  * ACHIEVEMENT_PRESET_ICONS unchanged (20 path'ей)
  * escapeAttr/escapeXml для защиты от XSS через кавычки в icon_url
  * CSS: новые --gold, --purple, --blue в :root (light+dark). Классы .ach-icon-wrap,
    .ach-glow-accent, .ach-glow-gold, .ach-pulse. @keyframes ach-pulse
  * achievements.html, profile_public.html: убрана внешняя <svg> обёртка
  * Тесты: /home/z/my-project/scripts/test_v210_phase2.py — 8 тестов прошли

- Фаза 3: Steam игры
  * steam.py: get_recently_played_games (GetRecentlyPlayedGames/v1),
    get_owned_games (GetOwnedGames/v1). Обе возвращают appid, name, playtime_forever_min,
    playtime_2weeks_min, icon_url
  * guild.py: таблица user_steam_games (user_discord_id, appid, name, playtime_forever_min,
    playtime_2weeks_min, icon_url, last_fetched_at). UNIQUE(user, appid)
  * db.py: STEAM_CACHE_TTL_SECONDS = 3600, g_get_steam_games_cached (возвращает (games, fresh)),
    g_save_steam_games_cache (DELETE + INSERT), g_get_user_steam_games (lazy refresh)
  * web.py: /u/{discord_id} — добавлены recent_steam_games, has_steam_linked в контекст
  * profile_public.html: новый блок "🎮 Steam за 2 недели" с играми, временем, иконками
  * Тесты: /home/z/my-project/scripts/test_v210_phase3.py — 7 тестов прошли

- Финальный smoke-test: все Python файлы синтаксически валидны, все ключевые функции
  и таблицы существуют

Stage Summary:
- 8 багов ачивок исправлено, 3 новых фичи добавлены (Steam игры, liquid glass иконки, безопасность)
- 21 тест всего (6+8+7), все прошли
- Ветка feature/v2.1.0-achievements-steam готова к PR

---
Task ID: v2.3.2
Agent: main
Task: v2.3.2 — большая итерация: UI доработки + Steam фичи + новые ачивки

Work Log:
- Прочитал worklog.md, запустил 2 Explore-агентов параллельно для аудита
  dashboard (panel.html), sidebar.html, profile_public.html, steam.py,
  db.py (achievements triggers, settings, games functions)
- Создал ветку feature/v2.3.2 из feature/v2.3.1-admin-panel

Доработка 1: Сайдбар — компактный футер
- Убраны ник, role-badge, "Настройки" текст из футера сайдбара
- Оставлена только кликабельная аватарка (36×36, с hover-зумом)
- Аватарка теперь ведёт на публичный профиль (/u/{discord_id}), а не на /profile
- Убрана отдельная кнопка 👁 (дубликат)

Доработка 2: Шестерёнка на аватарке публичного профиля
- Убрана отдельная кнопка "Настройки" рядом с "Назад"
- Аватар обёрнут в .profile-avatar-wrap (position: relative)
- При hover — появляется полупрозрачная шестерёнка с blur(3px) фоном
- Клик по шестерёнке → /profile (настройки), только для is_self

Доработка 3: Discord-статус на аватарке публичного профиля
- Добавлен .status-dot с цветом по discord_info.status (online/idle/dnd/offline)
- Использует тот же CSS-класс что на дашборде (.avatar-wrap.lg .status-dot)

#5: Дашборд — двухколоночный "Сейчас" + "Сейчас играют"
- "Быстрые действия" перенесены наверх (под status-bar админа, над "Сейчас")
- Блок "Сейчас" теперь двухколоночный (grid 1.4fr 1fr):
  * Левая: киновечер (collection / wheel / winner / empty)
  * Правая: "Сейчас играют" — список active_members с current_game
- Mobile: stack в одну колонку
- В блоке "Сейчас играют" — аватар 24×24 + статус-дот + имя + название игры
- Лимит 6 юзеров, если больше — "и ещё N"

#8: Сортировка игр по последним запущенным + "new" бейдж
- Добавлена колонка rtime_last_played INTEGER DEFAULT 0 в user_steam_games
  (миграция для всех существующих гильдий)
- steam.py: get_owned_games и get_recently_played_games теперь захватывают
  rtime_last_played из ответа Steam API
- db.py: g_save_steam_games_cache сохраняет rtime_last_played;
  g_get_steam_games_cached возвращает его
- g_get_user_games_merged переписан:
  * Сортировка по last_played_at DESC (сначала недавно запущенные)
  * Для Steam: last_played_at = rtime_last_played (Unix timestamp)
  * Для Discord-only: last_played_at = MAX(ended_at) из member_activities
  * Fallback: playtime_forever DESC (для never-played)
  * is_new = True если last_played_at в пределах последних 14 дней
- profile_public.html: <li class="game-new"> получает золотое свечение
  (text-shadow + animation keyframes new-game-glow 2.4s infinite)
- Плашка "new" (.game-new-badge) — золотая, со светящимся box-shadow

#4: Пересечение библиотек (замена простого count)
- game_compat.common_games (уже считается в db.g_get_game_compat) теперь
  отображается в шаблоне как expandable 2-column list
- Клик по compat-item toggles "expanded" класс, раскрывает/скрывает список
- Hint "раскрыть →" / "свернуть ↑" через CSS ::before

#11: show_wishlist toggle + Steam wishlist в публичном профиле
- Новая колонка users.show_wishlist INTEGER DEFAULT 0 (миграция)
- db.get_user_show_wishlist / set_user_show_wishlist хелперы
- POST /api/profile/show_wishlist — переключатель (требует Steam profile)
- В /profile (настройки) — чекбокс с предупреждением о приватном профиле
- JS toggleShowWishlist() с inline success/error feedback
- В /u/{discord_id} — если show_wishlist=True и Steam API доступен,
  запрашивается топ-10 вишлиста (steam.get_top_wishlist_games)
- Новый блок "Steam-вишлист" с капсулами, % отзывов, годом релиза

#2: Совместные игровые сессии (multiplayer + войс)
- steam.py: get_app_details(appid) — Steam Store API appdetails
- steam.py: is_multiplayer_game(appid) — cached (7 дней), проверяет
  категории Multi-player / Co-op / PvP / etc.
- db.py: g_find_appid_by_game_name — поиск appid в кеше user_steam_games
  по нормализованному имени (по всем гильдиям)
- bot.py: get_joint_play_sessions(guild_id) — сканирует все войс-каналы,
  группирует участников по current_game, для каждой группы >= 2 юзеров
  проверяет multiplayer-статус через Steam appdetails
- web.py: / route добавлен вызов get_joint_play_sessions, результат
  передаётся в шаблон как joint_sessions
- panel.html: новый блок "Вместе играют" с glassmorphism-карточками:
  * Имя игры + 'MP' бейдж если multiplayer подтверждён
  * Название войс-канала
  * User chips (аватар + имя) с hover-зумом
- CSS: .joint-sessions grid auto-fit minmax(260px, 1fr)

#7: Новые триггеры ачивок + фикс steam_play_time
- Новые триггеры:
  * steam_games_count — количество игр в библиотеке Steam
  * steam_play_time_specific — время в конкретной игре
    (icon_config.game_name, через g_get_game_play_time_specific)
  * voice_co_sessions — количество уникальных юзеров, с которыми юзер
    делил войс-канал (через g_get_voice_co_occurrence)
- Все новые триггеры добавлены в PERIODIC_TRIGGERS (_auto_achievements_loop)
- Фикс бага steam_play_time: было расхождение — фронт × 3600 (hours→seconds),
  а БД возвращала минуты. Теперь возвращает секунды (minutes × 60),
  консистентно с voice_time / game_play_time
- achievements.html: dropdown trigger_type расширен 3 новыми опциями
- TIME_TRIGGERS_EDIT и TIME_TRIGGERS обновлены — steam_play_time_specific
  теперь тоже time-триггер
- game-select-row показывается для game_play_time И steam_play_time_specific
- TRIGGERS_NEED_GAME set для будущего расширения

Stage Summary:
- 8 пунктов реализовано (3 доработки + 5 фич из списка пользователя)
- Изменено: db.py, steam.py, bot.py, web.py, guild.py, static/style.css,
  templates/sidebar.html, profile_public.html, profile.html, panel.html,
  achievements.html
- Все Python файлы синтаксически валидны (ast.parse)
- Все Jinja шаблоны компилируются
- Новые миграции БД: rtime_last_played в user_steam_games, show_wishlist в users
- Ветка feature/v2.3.2 готова к PR
