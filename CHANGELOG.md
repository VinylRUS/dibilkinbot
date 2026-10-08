# DeeBeelkin Bot — Changelog

## v2.2.0 (PR #69)
### Улучшения дизайна (без редизайна)
- Press-анимации на всех кнопках: при нажатии кнопка слегка сжимается (`scale(0.95)`) с пружинным возвратом (spring easing `cubic-bezier(0.34, 1.56, 0.64, 1)`). Чувствуется «живой» отклик вместо плоского клика
- Ripple-эффект: от точки клика расходится полупрозрачная волна — как на Android Material, но в стеклянном стиле. Применяется ко всем `.btn-primary` и `<button>`
- Hover-glow на карточках: при наведении на карточку (ачивки, статьи, профили) появляется мягкое акцентное свечение вокруг (`box-shadow` с `--accent-soft`)
- Spring-анимация на модалках: модалки выезжают с пружинным масштабированием (`scale(0.85) → scale(1)`) вместо резкого `display: flex`
- Toast-анимации: уведомления въезжают справа с пружиной и выезжают плавно при закрытии
- Sidebar items: при наведении сдвигаются на 2px вправо с glow, при нажатии сжимаются до `scale(0.97)`
- Bell icon: при наведении на колокольчик он слегка трясётся
- Контраст текста: добавлено лёгкое `text-shadow` на всех glass-карточках для лучшей читаемости
- Focus ring: при фокусе с клавиатуры появляется оранжевое кольцо вокруг элемента (`outline` с `--accent`)
- Мобильный blur: на экранах ≤768px размытие уменьшено до 12px (было 20px) — меньше нагрузка на GPU
- Fallback для `backdrop-filter`: если браузер не поддерживает размытие, карточки получают почти непрозрачный фон (92%) вместо эффекта стекла

## v2.1.4 (PR #67)
### Новое
- Модалка редактирования ачивки: теперь можно менять триггер, порог, цвет, glow и Discord-роль (раньше только название и описание). Дизайн компактный — все поля помещаются на одном экране без перегрузки. Красный крестик для закрытия с hover-эффектом

### Исправлено
- При выборе триггера «Игры (время игры)» dropdown триггера больше не «съедает» все варианты. Раньше `loadServerGames()` вызывался из `onTriggerChange()`, и `event.target` был равен `<select id="ach-trigger">` (на котором сработал `change`). Код думал что это кнопка «Обновить список игр» и заменял `innerHTML` select'а на спиннер — все `<option>` триггера исчезали. Теперь кнопка ищется только через `document.querySelector`, без `event.target`. Та же правка применена к `refreshDiscordRoles()`
- На дашборде больше не дублируется футер. Раньше был отдельный футер «🐝 DeeBeelkin v2.1.2» внутри `<main>` в `panel.html` + новый глобальный футер из `_footer.html` — два футера на одной странице. Старый удалён
- На странице «Колесо» (`/wheel`) теперь тоже отображается футер с версией — раньше `{% include "_footer.html" %}` отсутствовал в `wheel.html`
- В футере вместо `(unknown)` теперь показывается дата сборки (например `v2.1.4(20261008)`), если git недоступен на проде. Это даёт хотя бы понимание, какая версия кода задеплоена. Dockerfile поддерживает `--build-arg GIT_COMMIT=...` для точного hash при CI/CD сборке
- На мобилке (≤768px) лоты колеса теперь под колесом, а не справа за экраном. Раньше двухколоночный grid (1fr 320px) на узких экранах выводил список лотов за пределами viewport
- Пустой блок «Пока нет победителей» на /winners стал компактнее — меньше padding, меньше шрифт, занимает меньше вертикального места

## v2.1.2 (PR #66)
### Исправлено
- Футер с версией больше не «плавает» по экрану при прокрутке. Раньше он был `position: fixed` — катился вместе со скроллом и перекрывал контент. Теперь это статичный блок внизу страницы — остаётся на месте, видно только когда доскроллил до конца

## v2.1.1 (PR #65)
### Исправлено
- При выборе триггера «Игры (время игры)» список игр больше не исчезал. Legacy-код для старого кастомного dropdown скрывал нативный select через `display:none` — убрали его
- Выпадающий список теперь с чёрным текстом на белом фоне — раньше в тёмной теме мог быть тёмный текст на тёмном фоне
- В нижней части каждой страницы теперь отображается версия бота и хэш последнего коммита в формате `v2.1.1(abc1234)` — чтобы всегда видеть какая версия задеплоена

## v2.1.0
### Новое
- 🎮 Steam-игры в профиле: если у юзера привязан Steam-профиль, в его публичном профиле появился блок «Steam за 2 недели» с играми, временем за последние 2 недели и общим временем. Иконки игр подтягиваются с Steam CDN. Источник — официальный Steam Web API (GetOwnedGames), кеш на 1 час — повторные открытия профиля не дёргают API
- 🎨 Иконки ачивок переделаны в стиле glassmorphism панели: вместо плоского цветного кружка — стеклянная обёртка с размытием, акцентным кольцом и подсветкой сверху. Иконки теперь подстраиваются под dark/light тему автоматически (раньше были захардкожены под тёмную). Glow и pulse — реальные CSS-анимации, а не заглушки
- 🛡 Безопасность иконок: URL-иконки теперь валидируются по схеме (только http/https) — это закрывает потенциальный XSS через `javascript:` и SSRF через внутренние адреса. Скачанные файлы проверяются по Content-Type и magic bytes, чтобы не сохранить HTML error page под видом PNG

### Исправлено
- 🐛 Ачивки на «N оценок», «N цитат», «N в вишлисте» и подобные — больше не сломаны. Раньше фронт всегда умножал порог на 3600 (думал что это время в часах), и ачивка «10 оценок» сохранялась как порог 36000 — недостижимая цифра. Теперь конвертация в секунды применяется только для time-триггеров (войс, игры)
- 🐛 Discord-роль теперь автоматически выдаётся при авто-получении ачивки. Раньше роль выдавалась только при ручном «Выдать» через админку — автотриггеры (оценки, цитаты, игры) создавали запись в БД, но не дёргали Discord API. Теперь роль выдаётся в любом случае
- 🐛 Создание ачивки больше не подвешивает вкладку на 5 минут. Ретроспективная выдача (проверка всех юзеров, кто уже выполнил условия) теперь запускается в фоновой задаче и проверяет только активных юзеров текущей гильдии, а не всех зарегистрированных в боте
- 🐛 Ачивка «в бэклоге N фильмов» теперь действительно считает фильмы конкретного юзера, а не всей гильдии. Раньше условие `total >= N` срабатывало для всех одновременно
- 🐛 Повторная выдача ачивки больше не сбрасывает дату выдачи. Раньше каждый клик «Выдать» обновлял `granted_at` на текущее время — счётчик непросмотренных ачивок бесконечно загорался. Теперь `granted_at` обновляется только при реактивации отозванной ачивки
- 🐛 Отрицательный порог отклоняется при создании ачивки. Раньше `-5` означало «выдать всем без действий» (любой count ≥ -5 = True)
- 🐛 Админ (Трутень) теперь может привязывать Discord-роль к ачивке в конструкторе. Раньше кнопка «🔄 Обновить роли» и сам список ролей требовали прав суперпользователя — админ получал 403 без объяснений

### Техническое
- steam.py: новые функции `get_recently_played_games(steam_id64, api_key, count)` → IPlayerService/GetRecentlyPlayedGames/v1 и `get_owned_games(steam_id64, api_key)` → IPlayerService/GetOwnedGames/v1. Обе возвращают список dict'ов с appid, name, playtime_forever_min, playtime_2weeks_min, icon_url
- guild.py: новая таблица `guild_{id}_user_steam_games` (id PK, user_discord_id, appid, name, playtime_forever_min, playtime_2weeks_min, icon_url, last_fetched_at). UNIQUE(user, appid). Индексы по user и по (user, playtime_2weeks DESC)
- db.py: `STEAM_CACHE_TTL_SECONDS = 3600`. `g_get_steam_games_cached(guild_id, user_id, limit)` → (games, fresh) tuple. `g_save_steam_games_cache(guild_id, user_id, games)` — DELETE + INSERT (full replace). `g_get_user_steam_games(guild_id, user_id, limit, force_refresh)` — lazy refresh: если кеш свежий вернуть как есть, иначе запрос к Steam API; если API недоступен — вернуть устаревший кеш
- db.py: `list_guild_user_ids(guild_id)` — список discord_id юзеров активных в гильдии (через presence в watched/ratings/quotes/watchlist/user_achievements/voice_sessions/member_activities). Используется для ретро-выдачи ачивок
- db.py: `g_get_user_trigger_count` для `watched_count` теперь фильтрует по `watcher_user_id` (а не считает всю гильдию)
- db.py: `g_grant_achievement` UPDATE добавлен `AND is_active = 0` — реактивирует только отозванные ачивки
- db.py: `g_create_achievement` валидирует `trigger_threshold >= 0`
- db.py: `g_check_and_grant_auto` — убрано обязательное условие `bot_obj is not None` для выдачи роли (просто try/except с импортом bot)
- web.py: `_retro_grant_achievement` — фоновая async-функция через `asyncio.create_task`. Не блокирует HTTP-ответ
- web.py: `get_current_user` unchanged — `unread_achievements_count` считается как раньше
- web.py: `/api/achievements/discord_roles` и `/refresh` — `require_superuser` → `require_admin`
- web.py: `/api/achievements/create` скачивание иконок — валидация URL (urlparse scheme in http/https), magic bytes check (PNG/JPEG/GIF), путь сохранения `/app/data/icons/` (персистентный) + fallback-роут `/static/icons/custom/{filename}`
- web.py: `/u/{discord_id}` — добавлены `recent_steam_games` и `has_steam_linked` в контекст шаблона
- sidebar.html: `renderAchievementSVG` полностью переписан. Возвращает `<span class="ach-icon-wrap">` с backdrop-filter и SVG-иконкой внутри (вместо прежнего solid-fill кружка). `ACHIEVEMENT_COLORS` теперь хранит CSS-переменные (var(--accent), var(--red), etc.) — автоматически подстраивается под тему
- sidebar.html: добавлены `escapeAttr()` и `escapeXml()` для защиты от XSS через кавычки/скобки в `icon_url`
- style.css: новые CSS-переменные `--gold`, `--purple`, `--blue` (в light и dark темах). Новые классы `.ach-icon-wrap`, `.ach-glow-accent`, `.ach-glow-gold`, `.ach-pulse`. `@keyframes ach-pulse` (раньше был мёртвый класс без анимации)
- achievements.html: фронт конвертирует порог в секунды только для time-триггеров (voice_time*, game_play_time). Для count-триггеров передаёт как есть
- achievements.html, profile_public.html: убрана обёртка `<svg viewBox="0 0 64 64">${svg}</svg>` — `renderAchievementSVG` теперь возвращает готовый HTML с обёрткой
- profile_public.html: новый блок «🎮 Steam за 2 недели» с играми, временем, иконками. Заголовок «🎮 Последние игры» уточнён — «в Discord»

## v2.0.4
### Исправлено
- Создание ачивок: при ошибке сервера (БД недоступна, read-only FS, etc.) теперь показывается реальная причина вместо безликого «Ошибка создания». Бэкенд ловит все исключения (а не только ValueError) и возвращает JSON `{error: "ExceptionType: message"}`. Фронт пытается распарсить JSON даже на 500, fallback на текст ответа
- Создание ачивок: 422 валидация FastAPI теперь тоже отдаёт человекочитаемое `{error: "field: message"}` в дополнение к стандартному `detail` массиву. Глобальный `RequestValidationError` handler форматирует ошибки валидации в строку
- Фронт конструктора ачивок: защита от NaN в `trigger_threshold` (если input пустой или не-число — берём 0). `String(thresholdSeconds)` гарантирует что FormData не получит значение `NaN`
- Фронт конструктора ачивок: проверка обязательных полей `name` и `trigger_type` перед отправкой + логирование formData в console для диагностики
- Бэкенд: `invalid trigger_type` теперь возвращает список всех валидных триггеров в сообщении
- Фронт конструктора ачивок: `trigger_type`, `game_name`, `discord_role_id` читаются напрямую из DOM `select.value` (а не только из FormData). Кастомный dropdown (upgradeSelects) меняет `select.value` через JS property, и в некоторых сценариях FormData(form) не подхватывал это → юзер видел «Поле Триггер обязательно» хотя триггер был выбран
- Dropdown игр в конструкторе ачивок: показываем только название игры, без «(7 ч, 3 игр.)» в конце — чище и читабельнее

## v2.0.3
### Новое
- 🔔 Уведомления о новых достижениях: bell icon в шапке сайдбара показывает счётчик непросмотренных ачивок. При клике — переход в профиль + сброс бейджа. Анимированная тряска колокольчика + пульсация бейджа
- Toast при получении ачивки: после оценки фильма или записи цитаты, если сработал авто-триггер ачивки — показывается toast «🏆 Новая ачивка: %название%» (поддержка нескольких одновременно)
- Иконка ачивок в сайдбаре изменена с 🏆 (trophy) на 🏅 (medal) — раньше была одинаковая с «Победителями колеса»
- 🎮 Раздел «Последние игры» в профиле: показывает 5 последних сыгранных игр с длительностью и временем окончания. В отличие от блока «Топ игр» (который ранжирует по общему времени), здесь — хронологический список без агрегации
- 🎮 Совместимость по играм: плашка в профиле показывает % совпадения библиотек игр двух юзеров + список общих игр (до 3 названий). Считается как common / max(unique_a, unique_b) * 100
- Плашка совместимости фильмов уменьшена: убрана дублирующая строка «Вместе посмотрели: N», убран ветка `elif watched_together` (лишняя инфа), процент уменьшен с 2.2rem до 1.5rem, padding сокращён

### Техническое
- db.py: миграция users — добавлена колонка `last_viewed_achievements_at TEXT` (v2.0.3)
- db.py: g_count_unread_achievements(guild_id, user_discord_id) — COUNT user_achievements WHERE granted_at > users.last_viewed_achievements_at (или все активные если last_viewed IS NULL)
- db.py: g_mark_achievements_viewed(user_discord_id) — UPDATE users SET last_viewed_achievements_at = now()
- db.py: g_get_user_recent_games(guild_id, user_id, limit=5) — SELECT … ORDER BY ended_at DESC (без агрегации)
- db.py: g_get_game_compat(guild_id, user_a, user_b) — уникальные нормализованные имена каждого юзера + common = intersection, compatibility = common / max(unique_a, unique_b) * 100
- web.py: get_current_user обогащается полем `unread_achievements_count` (один быстрый SELECT COUNT на запрос)
- web.py: /api/winners/{id}/rate и /api/watched/{id}/rate теперь возвращают `new_achievements: [...]` (захватывают возврат g_check_and_grant_auto)
- web.py: /api/quotes теперь возвращает `new_achievements` (для записавшего цитату)
- web.py: новый POST /api/achievements/mark_read — сбрасывает unread-бейдж
- web.py: /u/{discord_id} — если юзер смотрит свой профиль, автоматически вызывается g_mark_achievements_viewed
- sidebar.html: bell icon в sidebar-header с badge, классы .sb-bell / .sb-bell-empty / .sb-bell-badge
- sidebar.html: JS window.markAchievementsRead(link) — POST /api/achievements/mark_read, window.notifyNewAchievements(list) — показывает toasts
- sidebar.html: icon_map achievements → 'medal' (было 'trophy'), emoji fallback 🏆 → 🏅
- style.css: .sb-bell (32px круг), .sb-bell-icon (1rem), .sb-bell-badge (абсолют, accent цвет, пульсация), @keyframes sb-bell-ring (тряска) и sb-bell-pulse (свечение бейджа)
- profile_public.html: taste-match-card уменьшен (padding 18→10px, percent 2.2→1.5rem, font-size sub 0.85→0.78rem). Убран блок «Вместе посмотрели» (elif ветка)
- profile_public.html: новый .compat-card-mini для game_compat (компактная плашка, 8px padding, 1.2rem percent)
- profile_public.html: новый блок «🎮 Последние игры» — список <ul class="recent-games-list"> с game-name, duration (мин), date
- winners.html / watched.html / quotes.html: вызов window.notifyNewAchievements(data.new_achievements) если в ответе есть массив

## v2.0.2
### Новое
- Роли Discord автоматически синхронизируются с БД при старте бота и при любом изменении ролей (создание/редактирование/удаление). Раньше роли подтягивались только по кнопке — теперь список всегда свежий
- Конструктор ачивок: роли подгружаются мгновенно при открытии страницы (из кеша БД). Кнопка «🔄 Обновить роли» теперь делает принудительную синхронизацию с Discord
- Дедупликация игр: «CS2», «CS2 » (с пробелом) и «cs2» теперь считаются одной игрой. При сохранении в БД имя нормализуется (strip + схлопывание множественных пробелов). В списке игр показывается самое длинное оригинальное написание
- Кнопка «🔄 Обновить роли» также запускает дедупликацию существующих записей игр в БД — покажет сколько дубликатов было схлопнуто
- g_get_game_play_time_specific теперь сравнивает по нормализованному имени — ачивки корректно срабатывают даже если юзер играл с другим написанием имени игры

### Техническое
- guild.py: новая таблица `guild_{id}_discord_roles` (role_id PK, name, color, position, hoisted, mentionable, permissions, synced_at). Индекс по position DESC
- db.py: g_sync_discord_roles(guild_id, roles) — INSERT OR REPLACE + удаление stale ролей. g_list_discord_roles(guild_id) — SELECT FROM cache
- db.py: g_dedupe_games(guild_id) — находит дубликаты игр по LOWER(TRIM(...)), приводит к каноническому виду (MAX(activity_name))
- db.py: _normalize_game_name(name) — strip + collapse whitespace, без смены регистра
- db.py: g_list_server_games — GROUP BY нормализованного имени, отображает MAX(activity_name) как «лучшее» написание
- bot.py: fetch_guild_roles теперь возвращает permissions.value. sync_guild_roles_to_db(guild_id) — обёртка над fetch + g_sync_discord_roles
- bot.py: on_ready вызывает sync_guild_roles_to_db для каждого guild после init_guild_tables. on_guild_join — тоже
- bot.py: новые обработчики on_guild_role_create / on_guild_role_update / on_guild_role_delete — пересинхронизируют кеш ролей
- bot.py: _handle_presence_change нормализует имена игр через db._normalize_game_name перед сохранением
- web.py: /api/achievements/discord_roles теперь читает из БД-кеша (мгновенно). Новый POST /api/achievements/discord_roles/refresh — принудительная синхронизация с Discord + дедупликация игр
- achievements.html: loadDiscordRoles() вызывается автоматически при открытии страницы. Кнопка «🔄 Обновить роли» дёргает refreshDiscordRoles() (POST /refresh). Кнопка игр переименована в «🔄 Обновить список игр» для консистентности

## v2.0.1
### Новое
- Индикатор статуса Discord на аватарках в дашборде — маленький кружок в правом нижнем углу (как в самом Discord): 🟢 онлайн, 🟡 AFK, 🔴 DND, ⚫ оффлайн
- Дашборд: 22 участника (2 ряда × 11), отсортированы по статусу — онлайн первыми, потом AFK, DND, оффлайн
- Профиль: статус показывает цветной кружок + текст вместо эмодзи. Стиль как индикаторы подключений на дашборде
- Ачивки: при выборе триггера «N сек играя в игры» появляется dropdown со списком игр с сервера (название + часы + сколько игроков). Можно выбрать конкретную игру или «Любая игра»
- API /api/achievements/server_games — список всех игр в которые играли на сервере

### Техническое
- style.css: .avatar-wrap (position relative), .status-dot (absolute bottom-right, 10px круг, border 2px), .status-dot.online #43B581 / .idle #FAA61A / .dnd #F04747 / .offline #747F8D. .avatar-wrap.sm для дашборда, .lg для профиля
- db.list_active_members_with_discord(limit=22): берёт 44 из БД (для запаса), web.py обогащает Discord статусами через get_member_discord_info, сортирует online→idle→dnd→offline, обрезает до 22
- db.g_list_server_games(guild_id): SELECT activity_name, SUM(duration), COUNT(DISTINCT user) FROM member_activities GROUP BY activity_name. Для dropdown в конструкторе ачивок
- db.g_get_game_play_time_specific(guild_id, user_id, game_name): время в конкретной игре. g_check_and_grant_auto: для game_play_time читает icon_config.game_name, если задана — проверяет конкретную игру
- web.py /api/achievements/create: принимает game_name, сохраняет в icon_config
- achievements.html: #game-select-row (display:none по умолчанию), показывается при trigger=game_play_time через onTriggerChange(). loadServerGames() → fetch /api/achievements/server_games
- panel.html: grid-template-columns: repeat(11, 1fr) для участников. avatar-wrap.sm с status-dot на каждой аватарке. title показывает игру/войс при hover
- profile_public.html: статус — цветной кружок 10px + box-shadow glow + текст. Убран эмодзи

## v2.0.0
### Новое
- Discord интеграция: автосоздание профилей для всех НЕ-ботов сервера при старте бота. При первом логине — предложение установить пароль (как и раньше)
- Live статус в профиле: 🟢/🟡/🔴/⚫ онлайн/нет на месте/DND/офлайн + текущая игра 🎮 + в каком войсе 🎤 + с кем
- Войс-статистика в профиле: общее время в войсе, количество сессий, время с другими и в одиночку
- «Чаще сидел в войсе с»: топ-3 юзера по времени совместного пребывания в войс-каналах + в какие игры при этом играли
- Топ игр: 3 любимые игры по времени игры
- Трекинг игр: on_presence_update записывает начало/конец игры (только playing, не Spotify/стримы/кастомный статус)
- Новые триггеры ачивок: N секунд в войсе (любых/одиночных/с другими), N секунд играя в игры

### Техническое
- bot.py: intents.presences + intents.voice_states включены. on_voice_state_update → voice_sessions. on_presence_update → member_activities. on_member_join → автосоздание профиля
- bot.py: _auto_create_member_profiles() при on_ready — перебирает все серверы, создаёт записи в users для НЕ-ботов
- bot.py: get_member_discord_info(discord_id, guild_id) — возвращает status, status_emoji, current_game, voice_channel, voice_with, joined_at, is_boosting
- guild.py: новые таблицы voice_sessions (user, guild, channel, joined_at, left_at, duration, was_solo, games_played) и member_activities (user, guild, activity_type, activity_name, started_at, ended_at, duration)
- db.py: g_get_voice_stats — total/solo/with_others/avg. g_get_voice_co_occurrence — топ-N юзеров по пересечению времени в войсе + игры. g_get_top_games — топ игр по времени. g_get_voice_time / g_get_game_play_time — для триггеров ачивок
- db.py: ACHIEVEMENT_TRIGGERS добавлены voice_time, voice_time_solo, voice_time_with_others, game_play_time
- db.py: g_get_user_trigger_count обновлён для новых триггеров (возвращает секунды, не количество)
- web.py: /u/{id} — discord_info, voice_stats, voice_co, top_games в контексте
- profile_public.html: блок Discord live (статус+игра+войс), блок войс-статистики, «чаще сидел с», топ игр
- achievements.html: 4 новых option в dropdown триггеров

## v1.9.7
### Новое
- /winners теперь показывает только реальные победители колеса (confirmed). Фильмы добавленные вручную в бэклог больше не появляются в победителях — они остаются в /watched с оценками, но не засоряют список победителей
- Блок «Участники» на дашборде — виден всем юзерам. Сетка аватаров с именами, клик → публичный профиль. Цвет рамки аватара показывает роль: оранжевая (Матка), зелёная (Трутень), серая (Пчела)
- Сортировка бэклога: выпадающий список «По дате» / «По оценке» в /watched. При выборе «По оценке» фильмы сортируются по средней оценке (топ вниз)
- Спиннер вместо '...' при отправке форм (добавление фильма в бэклог, добавление в вишлист). CSS .spinner-inline — вращающийся круг
- Редактирование ачивок: API /api/achievements/{id}/edit — обновить название и описание
- Ретроспективная выдача ачивок: при создании ачивки с триггером (не manual) проверяются все существующие юзеры — если кто-то уже выполнил условия, ачивка выдаётся автоматически
- Автоскачивание иконок: при создании ачивки с URL иконки (icons8 и др.) PNG скачивается в static/icons/custom_*.png. Больше не нужно дёргать внешний сервер при каждом рендере
- Отзывы к фильмам: необязательное текстовое поле при оценке (до 500 символов). Колонка review в ratings таблице. API /api/winners/{id}/rate и /api/watched/{id}/rate принимают параметр review
- Sidebar footer: аватарка выровнена по центру в свёрнутом режиме (убран padding-left)

### Техническое
- db.g_get_winners_with_ratings + g_count_winners: WHERE w.confidence = 'confirmed'. Виртуальные winners (unconfirmed) не показываются в /winners, но остаются в БД для оценок
- db.list_active_members(limit=12): новый запрос для дашборда. SELECT по last_login_at DESC
- db.g_list_watched: добавлен параметр sort ('recent' | 'rating'). ORDER BY avg_rating DESC при sort='rating'
- db.g_upsert_rating: добавлен параметр review. INSERT/UPDATE review колонки. ON CONFLICT обновляет review
- db.g_update_achievement(name, description): обновление названия/описания. db.g_update_achievement_icon_config: обновление icon_config после скачивания иконки
- guild.py ratings схема: rating REAL, добавлена колонка review TEXT
- db.init_db: миграция v1.9.7 — ALTER TABLE guild_{id}_ratings ADD COLUMN review TEXT
- web.py /api/achievements/create: после создания — ретроспективная проверка всех юзеров через g_check_and_grant_auto. Если URL иконки задан — httpx.get → сохранение в static/icons/custom_{hash}.png → обновление icon_config
- web.py /api/achievements/{id}/edit: новый endpoint (require_admin)
- web.py /watched: добавлен параметр sort, передаётся в g_list_watched и шаблон
- web.py /api/winners/{id}/rate + /api/watched/{id}/rate: принимают review параметр, передаётся в g_upsert_rating
- style.css: .spinner-inline (14px вращающийся круг, border 2px currentColor, animation 0.6s). .sb-user: padding 8px 0 в свёрнутом режиме (центрирование)
- watched.html: <select name="sort"> с опциями «По дате» / «По оценке». Кнопка отправки: btn.innerHTML = spinner вместо textContent = '...'
- profile.html: кнопка отправки вишлиста: spinner вместо '...'

## v1.9.6
### Новое
- 3-уровневая система ролей (пчелиная тематика): Матка (superuser, все возможности), Трутень (junior-admin, может создавать ачивки, удалять фильмы из бэклога и победителей, но НЕ может лезть в настройки бота), Пчела (обычный юзер). Роли видны в sidebar, профиле и списке юзеров

### Техническое
- db.py: миграция v1.9.6 — добавлена колонка role TEXT DEFAULT 'user' в таблицу users. Существующие is_admin=1 → 'superuser' (если env ADMIN_DISCORD_ID) или 'junior-admin'. is_admin=0 → 'user'
- db.py: get_user_role(discord_id) — возвращает 'superuser'/'junior-admin'/'user'. Env admin всегда superuser. ROLE_LABELS = {'superuser': 'Матка', 'junior-admin': 'Трутень', 'user': 'Пчела'}
- db.py: is_superuser(discord_id) — только superuser. is_admin(discord_id) — superuser ИЛИ junior-admin (backward compat). set_user_role(discord_id, role) — устанавливает роль, нельзя понизить env-admin
- db.py: set_admin() — legacy wrapper, конвертирует в set_user_role. get_user() и list_users() — добавлен role в SELECT
- db.py: get_notification_recipients() — SQL изменён с is_admin=1 на role IN ('superuser','junior-admin')
- web.py: require_superuser(request) — новая dependency для sensitive роутов. require_admin остаётся для junior-admin-доступных роутов
- web.py: session payload теперь содержит is_superuser, role, role_label рядом с is_admin
- web.py: роуты разделены: require_superuser = /tokens, /channels, /features, /guilds (approve/reject/delete), /users/{id}/admin (toggle), /api/achievements/discord_roles. require_admin = /achievements (create/delete/grant/revoke), /watched/{id}/delete, /winners/{id}/delete, /users (view), /api/wheel/clear, santa reveal/close, reset-password
- web.py: toggle_admin теперь использует set_user_role('junior-admin'/'user'). Superuser нельзя понизить через toggle
- sidebar.html: Токены/Каналы/Фичи/Серверы скрыты для junior-admin ({% if user.is_superuser %}). Ачивки/Юзеры видны любому админу. Badge: Матка (accent) / Трутень (green) / ничего
- users.html: распаковка 9-tuple (добавлен role). Badge: Матка/Трутень/Пчела. Toggle отключён для superuser (нельзя понизить env-admin)
- profile_public.html: badge показывает Матка (superuser) или Трутень (junior-admin)

## v1.9.5
### Новое
- Liquid glass иконки: sidebar навигация теперь использует полупрозрачные стеклянные иконки в стиле liquid-glass с icons8. При наведении — нежный тёплый glow (drop-shadow 3px rgba(255,183,3,0.25) + brightness 1.12), плавный переход 0.25s. Иконки ачивок в конструкторе тоже liquid glass
- Кастомные dropdown: все выпадающие списки на всех страницах заменены на div-based компоненты с glassmorphism стилем. Больше нет белого-на-белом в option. Компонент автоматически заменяет все <select> при загрузке страницы — не нужно менять каждый шаблон вручную

### Техническое
- style.css: добавлены стили .custom-select-wrapper, .custom-select-trigger, .custom-select-options, .custom-select-option. Trigger: appearance:none, кастомная SVG-стрелка через ::after, border-radius 12px. Options: absolute positioned, backdrop-filter blur, background var(--card-solid), max-height 240px, slide animation 0.15s. Option:hover → accent-soft, .selected → accent фон
- sidebar.html: window.upgradeSelects() — находит все <select>, скрывает оригинал (display:none), создаёт div-based копию. Синхронизация: custom → select (dispatchEvent change), select → custom (MutationObserver). Закрытие при клике вне. Запускается на DOMContentLoaded
- sidebar.html: sb_icon макрос переписан — вместо inline SVG использует <img> с icons8 liquid-glass URL. Fallback на эмодзи через onerror. icon_map: dashboard→home, backlog→checkmark, channels→video, features→settings, users→groups, guilds→parallel-tasks, logout→exit
- style.css: .sb-liquid-icon — opacity 0.75, transition 0.25s. .sb-item:hover → opacity 1, filter drop-shadow(0 0 3px rgba(255,183,3,0.25)) brightness(1.12). Эффект «тепления» — очень нежный
- achievements.html: preset иконки заменены на <img> с liquid-glass URL. onerror fallback на первые 2 буквы

## v1.9.4
### Новое
- Исправлены 4 критических бага в конструкторе ачивок, обнаруженные при ревью через браузер: иконки рендерились чёрными (SVG fill не поддерживал CSS-переменные), inline-скрипты не выполнялись, JSON обрезался в data-атрибутах, поле порога было видимо для ручных ачивок

### Техническое
- sidebar.html ACHIEVEMENT_COLORS: var(--accent) заменён на hex #FFB703 (SVG fill не поддерживает CSS-переменные). Аналогично для red→#E55A4E, green→#6FAE5A. ACHIEVEMENT_GLOW_COLORS тоже переведены на hex
- achievements.html: убран inline <script>renderAchievementIcon(...)</script> — заменён на data-icon-config атрибут + рендеринг через querySelectorAll. Тот же фикс в profile_public.html
- data-icon-config использует одинарные кавычки + |safe (двойные кавычки JSON конфликтовали с HTML-атрибутом)
- onTriggerChange() вызывается при инициализации — поле порога скрывается для manual/first_*

## v1.9.3
### Новое
- Иконки ачивок больше не вылезают за границы круга: preset SVG хранят только <path> элементы (без <svg> обёртки), что корректно масштабируется через transform внутри родительского SVG

### Техническое
- sidebar.html ACHIEVEMENT_PRESET_ICONS: изменён формат хранения с полного '<svg viewBox=...><path/></svg>' на только '<path d=.../>'. renderAchievementSVG оборачивает path в <g transform='translate(20,20) scale(0.9)' fill='white'>

## v1.9.2
### Новое
- Конструктор ачивок переработан: убраны формы (круг/щит/звезда/...) — теперь всегда кружок. Иконки выбираются из встроенной библиотеки SVG (20 пресетов: movie, star, trophy, film, popcorn, camera, crown, fire, diamond, heart, rocket, lightning, eye, check, users, quote, gift, santa, medal, target) или по URL (например с icons8). Все выпадающие списки (select) получили кастомный стиль под glassmorphism тему — больше нет белого-на-белом

### Техническое
- sidebar.html: window.ACHIEVEMENT_SHAPES убран (было 6 форм), window.renderAchievementSVG теперь рендерит только кружок. Иконка может быть preset (icon_key) или по URL (icon_url). URL приоритетнее. Добавлена window.ACHIEVEMENT_PRESET_ICONS с 20 SVG-иконками (как sidebar SVG). COLORS теперь хранит {svg, hex} — hex для генерации icons8 URL
- style.css: добавлены стили для select — appearance: none, кастомная SVG-стрелка через background-image (currentColor), color-scheme: dark light для нативного dropdown в тёмной теме. option получает явные background+color. Убран белый-на-белом
- achievements.html: убран shape-grid (6 форм), заменён на icon-grid с 20 preset SVG-иконками. Добавлено поле URL (input type=text) для кастомной иконки с icons8. Hint со ссылкой на icons8.ru и инструкцией. JS: builderConfig использует icon_key + icon_url вместо shape + emoji. URL приоритетнее preset при заполнении
- web.py /api/achievements/create: параметры icon_shape + icon_emoji заменены на icon_key + icon_url. icon_config хранит {icon_key, icon_url, color, glow}. Если оба пустые — дефолт icon_key='trophy'
- Тест: scripts/test_achievements_visual.py — генерирует HTML с 4 секциями (preset SVG, URL icons8, glow варианты, select) для визуальной проверки через VLM. VLM подтвердил: preset иконки белые в кружках, URL иконки загрузились, select стилизован под тёмную тему без белого-на-белом

## v1.9.1
### Новое
- Система ачивок: полноценная страница /achievements (только админ) с конструктором, где можно «собрать» иконку из слоёв (форма + эмодзи + цвет + glow), выбрать триггер авто-выдачи и привязать Discord роль. Ачивки отображаются в публичном профиле пользователя

### Техническое
- Схема БД: 2 новые таблицы guild_{id}_achievements (id, name, description, icon_config JSON, trigger_type, trigger_threshold, discord_role_id, is_active, created_at, created_by) и guild_{id}_user_achievements (id, achievement_id, user_discord_id, granted_at, granted_by, is_active, UNIQUE(achievement_id, user_discord_id)). Добавлены в GUILD_TABLES whitelist
- db.ACHIEVEMENT_TRIGGERS: множество допустимых триггеров — manual, ratings_count, quotes_count, watchlist_count, wheel_wins, collections_started, santa_participations, watched_count, first_rating, first_quote
- db.g_create_achievement(guild_id, name, description, icon_config dict, trigger_type, threshold, discord_role_id, created_by): создание шаблона. icon_config хранится как JSON
- db.g_list_achievements(guild_id, active_only=False): список всех ачивок гильдии с распакованным icon_config
- db.g_get_achievement(guild_id, ach_id): одна ачивка
- db.g_delete_achievement(guild_id, ach_id): каскадное удаление шаблона + всех user_achievements
- db.g_grant_achievement(guild_id, ach_id, user_discord_id, granted_by): INSERT OR IGNORE + UPDATE is_active=1 (idempotent, реактивирует отозванные). Возвращает False если ачивки не существует
- db.g_revoke_achievement(guild_id, ach_id, user_discord_id): UPDATE is_active=0 (не удаляет, сохраняет историю)
- db.g_list_user_achievements(guild_id, user_discord_id, active_only=True): JOIN user_achievements + achievements, возвращает [{achievement_id, granted_at, name, description, icon_config, trigger_type, discord_role_id}]
- db.g_count_user_achievements(guild_id, user_discord_id): COUNT активных ачивок
- db.g_has_achievement(guild_id, ach_id, user_discord_id): bool
- db.g_get_users_with_achievement(guild_id, ach_id): JOIN users для display_name/avatar_url
- db.g_get_user_trigger_count(guild_id, user_discord_id, trigger_type): считает счётчик для конкретного триггера (ratings → COUNT из ratings, quotes → COUNT из quotes WHERE recorded_by OR author_user_id, watchlist, wheel_wins через JOIN winners+wheel_items, collections_started, santa_participations, watched_count)
- db.g_check_and_grant_auto(guild_id, user_discord_id, trigger_type, bot_obj=None): проверяет все активные ачивки этого триггера, выдаёт если порог пройден и ачивки ещё нет. Для first_rating/first_quote threshold игнорируется (всегда 1). Если есть discord_role_id и передан bot_obj — вызывает bot.assign_role_to_member. Возвращает список выданных ачивок
- bot.assign_role_to_member(guild_id, user_discord_id, role_id): выдаёт Discord роль через member.add_roles. Проверяет что роль уже есть (idempotent). Логирует ошибки (Forbidden, HTTPException)
- bot.remove_role_from_member(guild_id, user_discord_id, role_id): снимает роль через member.remove_roles. Idempotent
- bot.fetch_guild_roles(guild_id): возвращает список ролей сервера [{id, name, color, position, hoisted, mentionable}]. Фильтрует @everyone и роль бота. Сортировка по position DESC
- web.py GET /achievements: страница админа. require_admin. Список ачивок с granted_count
- web.py POST /api/achievements/create: создание ачивки. Параметры: name, description, icon_shape, icon_emoji, icon_color, icon_glow, trigger_type, trigger_threshold, discord_role_id. Валидация trigger_type через ACHIEVEMENT_TRIGGERS
- web.py POST /api/achievements/{ach_id}/delete: каскадное удаление
- web.py POST /api/achievements/{ach_id}/grant: ручная выдача + Discord роль если есть. Параметр: user_discord_id
- web.py POST /api/achievements/{ach_id}/revoke: отзыв + снятие Discord роли
- web.py GET /api/achievements/discord_roles: список ролей сервера для выпадающего списка
- web.py GET /api/achievements/{ach_id}/users: список юзеров с конкретной ачивкой
- web.py /api/winners/{id}/rate и /api/watched/{id}/rate: после успешного g_upsert_rating вызывается g_check_and_grant_auto для trigger_type=ratings_count и first_rating
- web.py /api/quotes: после g_add_quote вызывается g_check_and_grant_auto для quotes_count и first_quote
- web.py /u/{discord_id}: добавлен user_achievements = g_list_user_achievements в контекст шаблона
- templates/achievements.html: страница-конструктор с 2 колонками. Слева — список существующих ачивок с SVG-иконками, тегами (trigger, role, manual, granted count), кнопками «Выдать» и «Удалить». Справа — sticky конструктор: живое SVG-превью, поля (name, description), option-grid для формы (6 форм), emoji-grid (30 эмодзи), color-grid (6 цветов), select для glow, select для trigger (10 опций), threshold input (скрыт для first_*/manual), select для Discord роли с кнопкой «Загрузить роли сервера», warning о manage_roles permission. Модалка выдачи с вводом Discord ID и списком получивших
- templates/sidebar.html: добавлена глобальная функция window.renderAchievementSVG(config) — рендерит SVG с shape (6 форм: circle, shield, star, hexagon, ribbon, badge), emoji, color (6: accent, gold, red, green, purple, blue), glow (4: none, accent, gold, pulse). Также ACHIEVEMENT_SHAPES, ACHIEVEMENT_COLORS, ACHIEVEMENT_GLOW_COLORS как глобальные константы. Добавлена иконка 'achievements' в макрос sb_icon
- templates/sidebar.html: в админ-секцию добавлена ссылка /achievements с SVG иконкой и подписью «Ачивки»
- templates/profile_public.html: новая секция «🏅 Достижения» между stats и recent_ratings. Каждая ачивка — карточка 80px с SVG-иконкой 48px и названием. Hover → lift + shadow. Использует window.renderAchievementSVG
- Тест: scripts/test_v191_achievements.py — 7 сценариев (create+list, manual grant/revoke, auto-grant ratings_count threshold=5, first_rating, list_user_achievements, users_with_achievement, delete cascade). Все 8 наборов тестов проходят

## v1.9.0
### Новое
- Публичный профиль пользователя: страница /u/{discord_id} видна всем залогиненным. Показывает аватар, display_name, роль, статистику (оценок, средняя оценка, цитат, в вишлисте), последние 5 оценок и 5 цитат. Если оба юзера оценили ≥3 общих фильма — показывается «Taste Match» с процентом совпадения вкусов и счётчиком «вместе посмотрели N фильмов»
- Кликабельность по всему боту: hover на badge «⭐ X.X · N оценок» в /watched и /winners открывает модалку «Кто как оценил» со списком юзеров и их оценок. Каждый юзер в списке кликабелен → его публичный профиль. Автор цитаты в /quotes кликабелен → профиль. Аватарки в админ-активности /panel и в списке /users — кликабельны → профиль

### Техническое
- db.g_get_user_stats(guild_id, user_discord_id): возвращает dict с ratings_count, avg_rating, watched_count, quotes_count, watchlist_count. 4 отдельных COUNT запроса (для каждой таблицы guild_{id}_*)
- db.g_get_user_recent_ratings(guild_id, user_discord_id, limit=5): последние N оценок юзера через JOIN ratings + winners по winner_id, ORDER BY updated_at DESC
- db.g_get_user_recent_quotes(guild_id, user_discord_id, limit=5): последние N цитат где юзер автор (author_user_id) ИЛИ записавший (recorded_by)
- db.g_get_taste_match(guild_id, user_a, user_b): JOIN оценок двух юзеров по winner_id, считает среднюю разницу (avg_diff), совместимость = 100 - (avg_diff/5)*100. Возвращает {common_count, compatibility, avg_diff}
- db.g_get_watched_together_count(guild_id, user_a, user_b): COUNT(*) FROM ratings a JOIN ratings b ON a.winner_id=b.winner_id WHERE a.user_discord_id=A AND b.user_discord_id=B
- db.g_get_ratings_for_winner(guild_id, winner_id): список оценок победителя с JOIN на users для display_name/avatar_url. Возвращает [{user_discord_id, rating, updated_at, username, display_name, avatar_url}]
- db.g_list_watched: добавлен winner_id в SELECT (LEFT JOIN winners). Возвращает 8-tuple (id, title, watched_at, rating, avg_rating, ratings_count, user_rating, winner_id) вместо прежнего 7-tuple. winner_id может быть None если фильм не был победителем колеса
- web.py GET /u/{discord_id}: новый маршрут публичного профиля. require_user (виден всем залогиненным). Возвращает target user, stats, recent_ratings, recent_quotes, taste_match с текущим юзером, watched_together count. Если юзер не найден — 404 с empty-state
- web.py GET /api/winners/{winner_id}/ratings_list: новый API для модалки «кто как оценил». Возвращает {ratings: [{user_discord_id, rating, updated_at, username, display_name, avatar_url}], count}
- web.py /panel admin_activity: добавлен discord_id в SQL (SELECT discord_id, username, display_name, last_login_at) и в dict. Теперь аватарка в списке активности кликабельна
- templates/profile_public.html: новый шаблон. Hero блок с аватаром 96px (или инициал в круге), display_name + badge ADMIN, мета (роль, сервер, @username). Stats grid 4 карточки. Taste match card с процентом 2.2rem. Recent ratings list и quote cards. Empty state «Пользователь не найден» если discord_id не существует
- templates/sidebar.html: добавлена глобальная функция window.showRatingsModal(winnerId, filmTitle) — создаёт модалку при первом вызове, fetch /api/winners/{id}/ratings_list, рендерит список юзеров с аватарами/оценками/датами. Каждый юзер кликабелен на /u/{discord_id}. closeRatingsModal() на Escape и клик вне модалки
- templates/watched.html: распаковка 8-tuple (добавлен winner_id), avg-rating-badge получает data-winner-id + onclick=showRatingsModal если winner_id есть. cursor: pointer на badge
- templates/winners.html: avg-rating + ratings-count обёрнуты в <a onclick=showRatingsModal>. title подсказка «Нажмите чтобы увидеть кто как оценил»
- templates/quotes.html: автор цитаты (аватар + имя + дата) обёрнут в <a href=/u/{author_user_id}> если есть author_user_id. Добавлен rel=noopener noreferrer для внешних ссылок
- templates/panel.html: activity-item обёрнут в <a href=/u/{discord_id}>. Empty state «😴 Нет активности» вместо простого текста
- templates/users.html: user-row обёрнут в <a href=/u/{discord_id}> вместо div
- Тест: scripts/test_v190_public_profile.py — 6 сценариев (stats, recent_ratings, taste_match, watched_together, ratings_for_winner, list_watched с winner_id). Существующий test_watched_ratings обновлён для 8-tuple

## v1.8.7
### Новое
- Шкала оценок изменена с 10-балльной на 5-балльную с половинками: 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0. Теперь 5 видимых звёзд, каждая может быть заполнена наполовину. Существующие оценки автоматически конвертированы: 10→5.0, 9→4.5, 8→4.0, 7→3.5, 6→3.0, 5→2.5, 4→2.0, 3→1.5, 2→1.0, 1→0.5. Пользователям не нужно ничего делать — оценки сохранятся

### Техническое
- db._is_valid_rating(rating): проверка 0.5-5 с шагом 0.5 (через rating * 2 должно быть целым 1-10). Принимает int (1..5) и float (0.5, 1.0, 1.5... 5.0)
- db.g_upsert_rating: тип параметра rating изменён с int на float, валидация через _is_valid_rating
- db.g_update_watched_rating: то же — float + валидация через _is_valid_rating (legacy, не используется с v1.8.4)
- db.upsert_rating (legacy без guild_id): float + валидация
- db.get_user_rating (legacy): возвращает float вместо int
- db.g_get_user_rating: возвращает float вместо int
- db.g_list_watched: нормализация user_rating через float() вместо int() (раньше 3.5 обрезалось до 3)
- db.init_db: добавлена миграция v1.8.7 (флаг migration_v187_done в global settings). Конвертирует ratings.rating и watched.rating по формуле new = old / 2.0 во всех guild_{id}_ratings, guild_{id}_watched, legacy ratings, legacy watched. Запускается один раз при первом старте после обновления
- web.py /api/watched/{id}/rate и /api/winners/{id}/rate: тип rating Form(int) → Form(float), валидация через db._is_valid_rating, error message: «rating must be 0.5-5 with 0.5 step»
- watched.html: цикл {% for i in range(1, 11) %} генерирует 10 кнопок-половинок с data-rating = i * 0.5 (0.5, 1.0, 1.5, ... 5.0). aria-label «Оценка X из 5». rating-value показывает «X/5» вместо «X/10»
- winners.html: 10 числовых кнопок 1-10 заменены на star-rating компонент (5 звёзд с половинками). Удалён stars-grid класс, добавлен star-rating с 10 кнопками-половинками. JS rateWinner обновлён для работы с float значениями через parseFloat. Кнопка «Удалить» получила класс btn-icon danger вместо delete-btn. avg-rating показывает «/5» вместо «/10». your-rating показывает «X/5» вместо «⭐×N X/10»
- watched.html JS updateStarsDisplay: parseFloat вместо parseInt, текст «X/5» вместо «X/10»
- Тесты: scripts/test_v187_rating_migration.py — 5 сценариев (валидация, формула миграции, upsert float, повторный init_db, g_list_watched с float user_rating). scripts/test_stars_visual.py — генерирует HTML со звёздами 0.5, 2.5, 4.0, 4.5, 5.0 для визуальной проверки через VLM (все 6 проверок прошли: 5.0→5 полных, 2.5→2+половинка, 4.0→4+пустая, 4.5→4+половинка, 0.5→половинка, без оценки→все пустые). Существующие тесты (test_race_conditions, test_watched_rate_flow) обновлены для использования float оценок

## v1.8.6
### Новое
- Дизайн-система кнопок: унифицированные классы .btn-primary, .btn-outline, .btn-danger, .btn-ghost, .btn-icon — заменили ~30 inline-стилей на кнопках. Главная CTA на дашборде (раньше рендерилась как синяя подчёркнутая ссылка) теперь корректная кнопка с акцентом
- Компактный рейтинг 5 звёзд с половинками вместо 10 числовых кнопок (1-10 значений): ~120px ширины вместо ~220px, привычный «звёздный» UX, корректно помещается на мобильном вместе с edit/delete
- Toast-уведомления: глобальная замена alert() на glassmorphism toast с auto-detect типа по эмодзи (✓→success, ❌/⚠→error). Появляется в правом нижнем углу, не блокирует UI, автоматически исчезает через 3.5 сек
- Empty states с иконками и CTA: вместо «font-style: italic; padding: 28px» теперь крупная иконка + заголовок + текст + кнопка действия. Применено к /watched и /winners
- Мобильная адаптивность winners/watched: на экранах ≤640px карточки переходят в одну колонку, действия переносятся вниз, звёзды не обрезаются

### Техническое
- CSS cleanup: удалён весь мёртвый код nav.top / .nav-group / .dropdown (~250 строк). style.css: 1271 → ~1080 строк. Никакой регрессии — nav.top не рендерится с v1.8.2 (заменён на sidebar)
- Объединены дублирующиеся правила article.card (transition + :hover) — было 2 одинаковых блока, остался 1
- Фикс CSS-переменных: в инлайн-стилях шаблонов использовались var(--card), var(--bg), var(--shadow) без их определения. На 9 страницах из 15 glassmorphism визуально ломался (карточки прозрачные, виден только border). Добавлены алиасы в :root и [data-theme="dark"]: --card: var(--glass-bg), --bg: var(--input-bg), --shadow: var(--glass-shadow)
- Добавлены классы кнопок: .btn-primary (для ссылок и <button>, accent + glow + lift on hover), .btn-outline (прозрачная с border), .btn-danger (red border, fill on hover), .btn-ghost (минимальная, для редких действий), .btn-icon (компактная 4×8px для ✏️/×/🔑), .btn-icon.danger (red variant)
- Toast JS: глобальная функция window.showToast(message, type, duration) с авто-детектом типа. Polyfill: window.alert перехватывается, короткие сообщения (<200 символов, без \n\n) идут через toast, длинные — через старый alert. Применено ко всем страницам через sidebar.html
- Compact star rating: 10 кнопок-половинок (по var(--star-size)/2 каждая), образующих 5 целых звёзд. Каждая половинка — отдельная <button> с overflow:hidden, символ ★ через ::before сдвинут (left:0 для левой половинки, left:calc(var(--star-size)/-2) для правой). Без налезания звёзд друг на друга. CSS через .star-rating с --star-size переменной. На мобильном --star-size уменьшается до 20px. aria-label на каждой кнопке для screen reader'ов
- Empty state CSS: .empty-state с .empty-state-icon (2.5rem, opacity 0.7), .empty-state-title (1.05rem bold), .empty-state-text (0.88rem soft), CTA через .btn-primary/.btn-outline
- Skeleton loading CSS: .skeleton с shimmer-анимацией (gradient + background-position animation, 1.4s infinite). Готов к использованию для плейсхолдеров при AJAX-загрузке
- Мобильная адаптивность: @media (max-width: 640px) — .winner-card (grid 2 колонки → 1), .watched-card (flex row → column), .watched-actions (justify-content: space-between, flex-wrap). !important для перебивания инлайн-стилей шаблонов
- panel.html: убран inline-стиль с .btn-primary «Новый сбор» → заменён на .btn-outline
- quotes.html: убран inline-стиль с #import-btn → заменён на .btn-outline
- watched.html: кнопки ✏️ и × получили класс .btn-icon и .btn-icon.danger + aria-label, звёзды заменены на .star-rating, alert() → showToast() для rateWatched и saveTitle
- winners.html: empty state заменён на .empty-state с иконкой 🎡 и CTA «К колесу →»
- Тест: scripts/test_css_vars.py — проверяет, что все var(--name) используемые в шаблонах определены в style.css. Запуск: python scripts/test_css_vars.py. Результат: 46 определено, 40 используется, 0 отсутствует

## v1.8.5
### Новое
- Минимальная длина пароля увеличена до 8 символов при установке нового пароля. Существующие пароли продолжат работать без изменений

### Техническое
- Bugfix: _send_backup_to_telegram падал с NameError: name 'now_msk' is not defined — функция использовала now_msk() в caption, но не импортировала её. Импорт был только в _db_backup_loop (внешняя функция). Бэкап БД создавался локально, но не отправлялся в TG. Добавлен import inside _send_backup_to_telegram
- Реинфорс хеширования паролей: PBKDF2-HMAC-SHA256 с 600 000 итераций (рекомендация OWASP 2023), формат pbkdf2_sha256$iter$salt_hex$hash_hex. Старый формат (1000 iter SHA-256 без HMAC) остаётся поддерживаемым для верификации существующих паролей. При успешной верификации legacy-пароля автоматически происходит пере-хеширование на новый формат (плавная миграция без требования пользователям менять пароли). password_salt для нового формата — NULL (salt внутри строки хеша)
- db._hash_password_legacy: оставлен только для верификации legacy-паролей. Использует 1000 итераций SHA-256 без HMAC (как было в v1.8.2)
- db._is_legacy_hash: определяет формат хеша по наличию префикса 'pbkdf2_'
- db.set_user_password: минимальная длина 8 символов (раньше 4), записывает новый формат, password_salt=NULL
- Race condition в db.g_get_or_create_winner_by_title: обёрнут в BEGIN IMMEDIATE, что сериализует писателей. Два одновременных вызова с одним title не создают дубликат — второй видит запись первого после блокировки
- Race condition в db.g_upsert_rating: обёрнут в BEGIN IMMEDIATE. INSERT rating и check-then-insert watched теперь атомарны — два одновременных вызова не создают дубликатов в watched (раньше watch мог получить две записи одного фильма). Убран try/except aiosqlite.IntegrityError (UNIQUE-индекса на lower(title) в guild_X_watched нет, поэтому catch никогда не срабатывал)
- Race condition в db.g_complete_collection_spin: обёрнут в BEGIN IMMEDIATE. UPDATE collections SET status='completed' теперь идёт ПЕРВЫМ (с проверкой WHERE status='active'). Если cur.rowcount == 0 — сбор уже завершён другим вызовом, возвращаем [] (веб-слой не загружает дубликаты в колесо)
- web.py /api/collection/start_spin: при пустом списке picks проверяет, что сбор действительно завершён (g_get_active_collection возвращает None), и возвращает ok=True с already_completed=True вместо ошибки. Пользователь увидит уже загруженное колесо
- Race condition в db.g_start_collection: обёрнут в BEGIN IMMEDIATE. Два одновременных POST /api/collection/start не создают два активных сбора
- Cookie secure=True: добавлен хелпер _cookie_secure_flag(request), который возвращает secure=True если запрос идёт по HTTPS (учитывает X-Forwarded-Proto для reverse-proxy). Применён ко всем 5 set_cookie вызовам (theme, session × 3, temp_session). На dev-сервере по HTTP cookie остаются без secure (иначе браузер их не принял бы)
- Шаблон set_password.html: обновлён текст ошибки и placeholder с «4 символа» на «8 символов», minlength=8
- web.py /set-password: минимальная длина 8 (раньше 4) — валидация до вызова db.set_user_password
- Тесты: scripts/test_password_migration.py (6 сценариев: новый формат, верификация, мин. длина, legacy верификация, авто-миграция, has_password/reset). scripts/test_race_conditions.py (4 сценария: g_get_or_create_winner_by_title, g_upsert_rating, g_start_collection, g_complete_collection_spin — все через asyncio.gather с 3-5 одновременными вызовами)

## v1.8.4
### Новое
- Per-user оценки в бэклоге: каждый участник ставит свою оценку фильму (1-10) на /watched, оценки суммируются и отображаются как средняя «⭐ X.X · N оценок». Каждый может переголосовать в любой момент — своя оценка подсвечивается активной звездой. Раньше оценка была одна на весь фильм и перезаписывалась последним голосовавшим — теперь каждый голос независимый
- Средняя оценка фильма в бэклоге: badge «⭐ X.X · N оценок» под каждым фильмом. Высчитывается из per-user оценок (через JOIN с winners по совпадению названия и ratings по winner_id). Badge обновляется мгновенно при голосовании без перезагрузки

### Техническое
- db.g_list_watched: LEFT JOIN с guild_X_winners (по lower(title) = lower(lot_name)) + коррелированные подзапросы к guild_X_ratings для avg_rating, ratings_count и user_rating. Возвращает 7-tuple (id, title, watched_at, rating, avg_rating, ratings_count, user_rating) вместо прежнего 4-tuple. user_discord_id передаётся как новый параметр
- db.g_get_or_create_winner_by_title: найти winner по названию (case-insensitive) или создать «виртуальный» unconfirmed winner. Используется при оценке фильма из /watched, чтобы привязать per-user оценку к ratings (для films, не бывших победителями колеса)
- /api/watched/{id}/rate переделан: теперь ищет title по watched_id → find/create winner → g_upsert_rating (per-user). Возвращает {user_rating, avg_rating, ratings_count} для обновления UI
- /api/watched/{id}/edit: параметр rating оставлен для обратной совместимости, но игнорируется — оценки теперь только через /rate
- watched.html: звёзды используют user_rating (а не общую rating), badge обновляется JS без перезагрузки, добавлена функция pluralRatings для склонения «оценка/оценки/оценок»

## v1.8.3
### Исправлено
- Колесо не крутилось если был недавний победитель: блокировка срабатывала даже когда в колесе ещё оставались фильмы. Теперь блокировка только если в колесе меньше 2 элементов
- Кнопка «Загрузить из списков желаемого» убрана с /wheel — она дублировала лоты при повторном нажатии. Фильмы загружаются только через сбор на /movienight
- Пустое состояние колеса: «Начните сбор фильмов на /movienight» вместо «Загрузите из списков желаемого»

### Техническое
- /api/wheel/spin и /api/wheel/spin_elimination: проверка winner_already_determined теперь учитывает количество элементов в колесе (если ≥2 — крутить можно)
- Убрана JS функция loadFromWatchlist() из wheel.html
- Убран HTML кнопки load-watchlist-btn из wheel.html

## v1.8.2
### Новое
- Двухшаговый логин с паролем: при первом входе по Discord ID юзеру предлагается установить пароль. При последующих входах — ввод Discord ID и пароля на одной странице (поле пароля появляется через JS). Старые юзеры без пароля при первом входе после обновления увидят страницу установки пароля
- Кнопка сброса пароля в админ-панели юзеров: админ может сбросить пароль любого участника, юзер получит DM в Discord с уведомлением
- Постеры фильмов при наведении в бэклоге: при hover на название фильма показывается tooltip с постером. Cache-first — если постер уже в БД (movie_meta), 0 запросов к Кинопоиску. Если не найден — 1 запрос, кешируется на 7 дней

### Улучшено
- Страница логина переработана: убрано «пароль только для админа», двухшаговая форма (Discord ID → пароль), glassmorphism стиль
- Навбар заменён на выдвижную шторку справа: только иконки в свёрнутом виде, иконки + текст при hover. SVG иконки Lucide-style для всех пунктов. Кнопка выхода в подвале сайдбара рядом с переключателем темы
- Дашборд переработан: убраны лента активности, последние победители, последние просмотренные и топ цитат. Вместо них — персонализированный контент: «Сейчас» (статус киновечера), «Ваш профиль» (чек-лист настроек), «Быстрые действия». Админ видит дополнительно: активность участников, неоценённые победители, статистику сервера
- Анимированные индикаторы подключений: зелёный круг с расходящимся кольцом (pulse-ring), вместо примитивных кружочков
- Мобильная адаптивность: бургер-кнопка для меню, overlay с затемнением, компактные карточки

### Техническое
- Исправлен спам в логах: кеш версии бота (CHANGELOG.md парсится один раз при старте, не при каждом запросе к дашборду). Убраны вызовы init_guild_tables из всех route'ов кроме login (таблицы создаются при on_ready бота)
- Пароли хешируются через sha256 + случайный salt (1000 итераций). Не используется bcrypt — чтобы не добавлять зависимость и не нагружать процессор
- Новые функции db.py: set_user_password, verify_user_password, has_password, reset_user_password, get_posters_for_titles
- Новый endpoint /api/movie/poster — cache-first получение постера по названию
- Новый endpoint /api/users/{id}/reset-password — сброс пароля (админ)
- Новые страницы: /set-password (установка пароля), sidebar.html (замена _nav.html)
- Миграция БД: 2 новые колонки в users (password_hash, password_salt)

## v1.8.1
### Улучшено
- Полный редизайн веб-панели в стиле Glassmorphism: полупрозрачные карточки с blur-эффектом, размытые цветные пятна на фоне, мягкие многослойные тени, внутренние блики на стёклах
- Тёмная тема переработана: глубокий тёмно-синий градиент вместо чёрного, медовые акценты с glow-эффектом, полупрозрачные стеклянные панели
- Светлая тема: усиленные цветные blobs на фоне (медовый + оранжевый), более прозрачные карточки для лучшего эффекта стекла
- Все интерактивные элементы (кнопки, поля ввода, пагинация, переключатели) получили плавные переходы и hover-эффекты с подъёмом
- Навбар стал стеклянным с blur-эффектом, активные пункты подсвечены медовым акцентом
- Мобильная адаптивность: навбар сворачивается, карточки растягиваются на всю ширину, уменьшенные отступы
- Кастомный скроллбар в тёмной теме, подсветка выделенного текста
- Увеличены скругления (16-20px), добавлены glow-эффекты на акцентных элементах

### Техническое
- style.css полностью переписан (896 строк вместо 569): CSS-переменные для glass-эффектов, два набора переменных для light/dark тем, media queries для мобильных

## v1.8.0
### Новое
- Реал-тайм список участников на странице киновечера с аватарами: карточки участников в сайдбаре обновляются через WebSocket, готовые подсвечиваются зелёной рамкой и glow-эффектом
- Еженедельный бэкап базы данных в Telegram: раз в неделю (воскресенье 04:00 МСК) бот создаёт дамп SQLite и отправляет файлом админу в личку. Если админ не привязал TG — бэкап сохраняется локально с предупреждением в логе

### Улучшено
- Страница киновечера: карточки участников стали карточками (были строки), с анимацией перехода при готовности

### Техническое
- Улучшил стабильность и поправил пару багов

## v1.7.4
### Новое
- Персональные уведомления в Telegram: бот теперь шлёт личные сообщения организатору сбора и админам (с привязанным TG) о ключевых событиях киновечера. Уведомления приходят в личку каждому получателю отдельно, не в общий чат
- Три типа персональных уведомлений: «Победитель колеса» (с постером и метаданными если есть), «Сбор фильмов начат — жди участников», «Все готовы — можно начинать крутить». Победитель приходит с inline-кнопкой «Оценить в панели» (если задан panel_base_url)
- Тумблеры уведомлений в /profile: каждый юзер сам включает нужные типы уведомлений — по умолчанию все выключены. Доступно только если привязан TG. Сохранение через AJAX без перезагрузки
- Приветствие при привязке TG: после успешной привязки бот отвечает с подробным сообщением — какие уведомления доступны, ссылка на веб-панель профиля, напоминание что тумблеры по умолчанию выключены
- Пинг в Discord если организатор не привязал TG: при старте сбора бот шлёт ephemeral в личку Discord с предложением использовать /linktg

### Улучшено
- Страница /channels полностью переработана: поля сгруппированы по секциям (веб-панель, Discord каналы, Telegram чаты), добавлены подробные подсказки с примерами, объяснено что число «3» в ссылке t.me/c/4484643620/3 — это ID темы форума, а не chat_id
- Валидация chat_id в /channels: проверка формата прямо в браузере — предупреждает если введено число без префикса -100 (например, "3" вместо "-1004484643620"), подсвечивает поле красным, показывает конкретное сообщение об ошибке
- Валидация thread_id: проверка что это положительное число

### Техническое
- Миграция БД: добавлены 3 колонки в таблицу users (tg_notify_winner, tg_notify_collection_started, tg_notify_collection_ready) — все INTEGER DEFAULT 0
- Новые функции db.py: get_user_tg_settings, set_user_tg_setting, get_notification_recipients (находит организатора + админов с включёнными тумблерами и привязкой TG), is_tg_linked
- telegram.py: send_message теперь принимает reply_markup (inline-кнопки) — раньше только send_photo умел
- bot.py: 4 новых метода на KinovecherBot — _notify_organizers (базовый), notify_wheel_winner, notify_collection_started, notify_collection_all_ready, ping_unlinked_organizer
- web.py: /api/collection/start после успеха вызывает notify_collection_started или ping_unlinked_organizer. /api/collection/save_picks и /api/collection/set_ready проверяют «все готовы» и если да — вызывают notify_collection_all_ready
- Новый endpoint POST /api/profile/tg_settings для сохранения тумблеров
- on_wheel_spin_completed теперь вызывает notify_wheel_winner (в дополнение к посту в TG-чат)
- Smoke-тест: 8 тест-кейсов покрывают логику recipients (админ с тумблером, организатор с тумблером, юзер без тумблера, без привязки)

## v1.7.3
### Новое
- Баннеры статуса на странице колеса: над заголовком колеса теперь показывается компактная плашка с актуальным состоянием — пустое колесо, фильмы загружены вручную, идёт активный сбор, колесо загружено из сбора, или победитель уже определён. Каждый баннер содержит краткое объяснение и кнопку-ссылку для перехода на /movienight если нужно начать или продолжить сбор
- Блокировка повторной крутки: после определения победителя (как в классическом режиме, так и в режиме на выбывание) повторная крутка невозможна. Сервер возвращает понятную ошибку с именем победителя, клиент показывает alert и предлагает перейти на /movienight для нового сбора
- Блокировка крутки во время активного сбора: нельзя крутить колесо пока идёт активный сбор фильмов — сначала надо завершить сбор на странице /movienight, выбранные фильмы загрузятся в колесо автоматически
- Кнопка «Перейти к колесу» на странице киновечера: после завершения сбора организатор больше не редиректится автоматически на /wheel — вместо этого появляется блок «Сбор завершён» с кнопкой-ссылкой «Перейти к колесу», и организатор сам решает когда перейти и крутить

### Улучшено
- Компактный баннер статуса: первая итерация баннера занимала слишком много места и сдвигала колесо вниз — уменьшены отступы, шрифты, иконки, текст теперь в одну строку, баннер центрируется вместе с колесом
- Кнопка «Загрузить из списков желаемого» на странице колеса скрывается если идёт активный сбор или уже определён победитель — чтобы не путать организатора

### Техническое
- Новые функции db.py: g_get_last_completed_collection(guild_id) — последний завершённый сбор для баннера; g_get_recent_winner(guild_id, since_minutes=60) — последний победитель за N минут для блокировки повторной крутки
- /api/wheel/spin и /api/wheel/spin_elimination теперь проверяют два условия: активный сбор существует (active_collection_exists) и недавний победитель (winner_already_determined) — обе ошибки возвращают понятные сообщения с error_code для клиентской обработки
- movienight.html: вместо автоперехода на /wheel после start_spin вызывается showCollectionCompleted(picksCount) — заменяет organizer-controls на блок с кнопкой-ссылкой, скрывает прогресс-бар и список выбора
- WebSocket event collection_completed теперь передаёт picks_count — клиенты показывают счётчик загруженных фильмов
- Smoke-тест (10 шагов): проверены все 5 состояний баннера + блокировка крутки в обоих режимах

## v1.7.2
### Новое
- Сбор фильмов перед киновечером: новая страница /movienight — любой участник может запустить сбор, бот постит в Discord ссылку для участников, каждый выбирает ровно N фильмов из своего списка желаемого и жмёт «Готов». Организатор в реальном времени видит кто готов, но не видит какие именно фильмы выбрали (эффект сюрприза для колеса). При старте крутки все выбранные фильмы перемешиваются и загружаются в колесо
- Защита от дублей в списке желаемого: при добавлении фильма проверяется через Кинопоиск (по tmdb_id) — если фильм уже в списке у другого участника, бот сообщает «уже кем-то добавлен» и не создаёт дубликат. Это сохраняет сюрприз для колеса — нельзя иметь один фильм у двух людей сразу
- Поиск по бэклогу: на странице /watched появилось поле поиска, фильтрует по названию фильма
- Поиск по победителям: на странице /winners тоже появилось поле поиска
- Организатор киновечера может кикнуть участника (его выборы удаляются, фильмы остаются в вишлисте) или запустить крутку принудительно (без ожидания неготовых)

### Улучшено
- Пагинация по страницам: бэклог, победители и цитаты теперь разбиты на страницы 1, 2, 3… с красивыми кнопками ← Назад / Вперёд → и подсветкой текущей страницы (раньше была «вытесняющая» пагинация с кнопкой «показать ещё»)
- Время в команде /movienight теперь вводится по МСК (раньше по UTC) — бот сам конвертирует в UTC для Discord Scheduled Event, в анонсе пишется «МСК»
- Навбар: добавлен пункт «Киновечер» в выпадающее меню «Комьюнити»

### Техническое
- Удалён мёртвый код: WheelCog (/wheel add/list/remove), FilmNightCog (/filmnight), api_filmnight_* endpoints, небезопасный роут /download/{filename} (без auth — любой мог скачать файлы), дублирующие алиасы в db.py
- Исправлены баги: on_raw_reaction_add теперь не падает в DM (message.guild может быть None), _build_winner_embed ищет участника по всем серверам бота (не только по первому), убран хардкод https://dibilkis.bothost.tech (используется только panel_base_url из настроек)
- Новые таблицы в БД: guild_{id}_collections, guild_{id}_collection_participants, guild_{id}_collection_picks (сбор фильмов + участники + выбор)
- WebSocket events для сбора: collection_started, participant_joined/ready/kicked, picks_updated, collection_completed/cancelled
- Добавлен smoke-тест скрипт scripts/smoke_test.sh для локальной проверки endpoint'ов без discord-бота

## v1.7.1
### Новое
- Список желаемого: личный список фильмов для каждого участника, управляется через /addfilm, /delfilm, /myfilms в Discord (видны только автору) и через страницу /profile в веб-панели (добавление, удаление, редактирование названия)
- Колесо: кнопка «Загрузить из списков желаемого» — заполняет колесо всеми непросмотренными фильмами из списков всех участников
- Победитель колеса: автоматически помечается как «просмотрен» в списках желаемого всех, у кого он был
- Время по МСК: все даты и время в панели и Discord отображаются по московскому времени (UTC+3)

### Улучшено
- Бэклог: пагинация по 20 фильмов с кнопкой «Показать ещё»
- Убраны анимации появления с больших панелей для ускорения загрузки
- Колесо: убрана система сбора фильмов (filmnight) — список формируется постоянно
- Профиль: страница /profile теперь показывает список желаемого с кнопками редактирования и удаления

## v1.7.0
### Новое
- Редактирование названия фильма в бэклоге: нажмите ✏️ рядом с фильмом чтобы переименовать
- Оценка фильмов в бэклоге: кнопки 1-10 рядом с каждым фильмом, работают без перезагрузки
- Переключатель режима колеса: «Классический» / «На выбывание» в одной кнопке-переключателе
- Настройка времени вращения колеса: ползунок от 3 до 15 секунд

### Улучшено
- Навбар: фиксированная высота кнопок (32px) — Комьюнити, Бот и тема больше не сползают
- Бэклог: курсор автоматически возвращается в поле ввода после добавления фильма

## v1.6.5
### Новое
- Ручное добавление фильмов в бэклог: любой участник может добавить просмотренный фильм через форму на странице /watched

### Улучшено
- Навбар: иконка пчелы, компактные размеры, клик на лого возвращает на дашборд
- Канал обновлений: постинг changelog при новой версии, с фильтром технических деталей
- Версия бота в футере панели
- Команда /changelog показывает что нового

## v1.6.0
### Новое
- Авто-захват цитат: бот читает сообщения в канале #цитатник и сам сохраняет их как цитаты с embed
- Захват цитат реакцией: поставь эмодзи (по умолчанию 🗣️) на любое сообщение в любом канале — бот сохранит как цитату
- Команда /setquoteemoji — смена эмодзи для захвата цитат (например /setquoteemoji 💬)
- Команда /filmnight start/end/status — управление сбором фильмов для киновечера из Discord
- Команда /changelog — показать текущую версию бота и список новых функций
- Авто-анонс обновлений: при новой версии бот сам постит changelog в настроенный канал
- Колесо «на выбывание»: каждый спин убирает один фильм, последний оставшийся — победитель
- Рейтинги: любой участник может оценить фильм от 1 до 10, виден средний балл
- Импорт цитат из Discord-канала в веб-панели одной кнопкой
- Экспорт цитат в TXT (только текст, без авторов)
- Telegram: пост победителя с постером фильма, описанием и кнопкой «Оценить»
- Поддержка Telegram Topics (тем форума) — посты уходят в нужную тему
- Несколько Discord-серверов: каждый сервер имеет свои цитаты, победители и бэклог
- Навбар с выпадающими меню: «Комьюнити» и «Бот»
- Анонимность: кто добавил фильм в колесо, раскрывается только при победе
- Ping-статистика: дашборд показывает статус соединения с Discord и Telegram

### Улучшено
- Навбар перегруппирован: «Комьюнити» (Колесо, Победители, Бэклог, Цитаты) и «Бот» (Токены, Каналы, Фичи, Юзеры, Серверы)
- Карточки дашборда с анимацией появления и hover-эффектами
- Логотип в навбаре кликабелен — возвращает на дашборд
- Версия бота отображается в футере панели

### Техническое
- Dockerfile: uv вместо pip — деплой в 10-100x быстрее
- Переход на kinopoiskapiunofficial.tech вместо kinopoisk.dev
- Multi-tenant архитектура: изоляция данных между серверами через guild_id
- Удалён мёртвый код: pointauc.py, pointauc_watcher.py, tmdb.py
