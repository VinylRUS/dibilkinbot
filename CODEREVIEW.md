# Полное код-ревью v1.8.4 — сводный отчёт

Дата: 2026-10-04
Ветка: feature/v1.8.4-avg-rating
Объём: 15 859 строк (Python 7 094, HTML 7 337, CSS 1 271)

## Сводная таблица критичности

| Severity | db.py | web.py | bot/guild/cfg/main/ws | templates/css | **Итого** |
|----------|-------|--------|------------------------|---------------|-----------|
| CRITICAL | 4 | 4 | 9 | 1 | **18** |
| HIGH | 6 | 6 | 11 | 4 | **27** |
| MEDIUM | 9 | 8 | 14 | 9 | **40** |
| LOW | 9 | 9 | 10 | 8 | **36** |
| **Итого** | **28** | **27** | **44** | **22** | **121** |

---

## TOP-10 КРИТИЧЕСКИХ ПРОБЛЕМ (по приоритету фикса)

### 1. Multi-tenant изоляция сломана (CRITICAL, web.py + bot.py)
**Где:** `bot.py:53-60` `is_guild_member(discord_id)` обходит ВСЕ guilds бота и возвращает первый совпавший. В web.py `get_user_guilds` на это опирается.
**Эксплойт:** Юзер — участник ЛЮБОГО сервера с ботом может через `/select_guild` переключить `current_guild_id` на любой approved-guild и получить полный доступ: /watched, /winners, /quotes, крутить колесо, запускать сборы.
**Фикс:** `is_guild_member(discord_id, guild_id)` — проверка в конкретном guild'е.

### 2. `is_admin` глобальный, не per-guild (CRITICAL, db.py + web.py)
**Где:** `db.py:518-533` — один флаг `is_admin` в таблице `users` без guild_id. Кешируется в signed-cookie на 7 дней.
**Эксплойт:** Админ guild A → админ ВСЕХ guild'ов. После понижения прав — остаётся админом 7 дней пока сессия жива.
**Фикс:** Перенести в `guild_members.is_admin` (per-guild). В `require_admin` проверять per-guild, не кеш.

### 3. WebSocket `/ws/wheel` без auth + утечка `added_by` (CRITICAL, web.py + ws_manager.py)
**Где:** `web.py:2680-2710` — нет cookie-проверки, нет origin-валидации. `broadcast_spin_result` шлёт полный winner dict (с `added_by`).
**Эксплойт:** Любой подключается к `/ws/wheel?guild_id=0`, слушает все спины, видит кто добавил фильм.
**Фикс:** Валидировать session cookie в handshake, origin whitelist, анонимизировать broadcast.

### 4. Слабое хеширование паролей (CRITICAL, db.py:784-789)
**Где:** 1000 итераций SHA-256 без HMAC. Минимальная длина пароля 4 символа.
**Эксплойт:** GPU брутфорс ~10⁹ хешей/сек. 8-символьный пароль ломается за дни.
**Фикс:** PBKDF2-HMAC-SHA256 600k iter или argon2id. Минимальная длина 8.

### 5. Brute-force на `/login` без rate-limiting (CRITICAL, web.py:231-383)
**Где:** Ни per-IP, ни per-discord_id лимитов.
**Фикс:** slowapi или token bucket, 5 попыток / 10 минут / per discord_id + per IP.

### 6. Race condition в `g_get_or_create_winner_by_title` (CRITICAL, db.py:1449-1471)
**Где:** SELECT → INSERT без транзакции. `winners` не имеет UNIQUE на `lower(lot_name)`.
**Эксплойт:** Два одновременных POST `/api/watched/{id}/rate` для одного фильма → два winner'а → `g_list_watched` показывает произвольный avg/count (через `win.id` после GROUP BY).
**Фикс:** `BEGIN IMMEDIATE` или `INSERT ... ON CONFLICT(lower(lot_name)) DO UPDATE ... RETURNING id`.

### 7. Race condition в `g_complete_collection_spin` (CRITICAL, db.py:2023-2058)
**Где:** SELECT picks → UPDATE. Если `cur.rowcount == 0` (сбор уже завершён) — `picks` всё равно возвращается, web.py грузит их в колесо как дубликаты.
**Фикс:** Проверять `cur.rowcount` от UPDATE.

### 8. Multi-tenant утечка в `notify_wheel_winner` (CRITICAL, bot.py:791)
**Где:** `guild_id = 0  # TODO: когда будет multi-guild` — организатор берётся из guild_0 вместо текущего.
**Фикс:** `on_wheel_spin_completed(winner, guild_id)` принимает guild_id от вызывающего.

### 9. CSRF: ни одна из 15 POST-форм не имеет CSRF-токена (HIGH, все шаблоны)
**Где:** `/watched/{id}/delete`, `/winners/{id}/delete`, `/users/{id}/admin`, `/api/tokens`, `/api/channels`, `/api/features`, `/select_guild`, etc.
**Эксплойт:** Злоумышленник через `<form action="https://panel/users/123/admin" method="POST">` на стороннем сайте повышает себя до админа (SameSite=Lax не защищает top-level navigation для POST-форм).
**Фикс:** fastapi-csrf-protect или middleware + `{% csrf_token %}` в каждом шаблоне.

### 10. XSS через `onsubmit="confirm('...{{ user_var }}...')"` (CRITICAL, 4 шаблона)
**Где:** `guilds.html:154`, `watched.html:273`, `quotes.html:250`, `winners.html:198`.
**Эксплойт:** Имя фильма `');alert('xss` выполняется — Jinja2 autoescape декодирует `&#39;` обратно в `'` при чтении атрибута.
**Фикс:** `data-confirm-text="{{ title }}"` + JS-слушатель, не интерполировать в JS-строки.

---

## Проблемы по файлам

### db.py (2548 строк)
- **C1** Слабое хеширование паролей (784-789)
- **C2** Race в `g_get_or_create_winner_by_title` (1449-1471)
- **C3** Race в `g_complete_collection_spin` (2023-2058)
- **C4** Dead `IntegrityError` catch в `g_upsert_rating` (1574-1585) — UNIQUE на `lower(title)` отсутствует, дубликаты в watched
- **H1** Timezone-баг: `datetime.utcnow().timestamp()` (3 места) — на non-UTC сервере смещает cutoff
- **H2** `g_list_watched` — non-deterministic `win.id` после GROUP BY (может показывать чужие рейтинги)
- **H3** Race в `verify_tg_link` (582-601)
- **H4** Race в `g_start_collection` (1731-1758)
- **H5** Race в `g_add_collection_participant`, `g_add_santa_participant`, `g_add_to_watchlist`
- **H6** Отсутствие индексов на `lower(title)`, `lower(lot_name)` — full table scans
- **M1** ~900 строк мёртвого кода (legacy non-g_ функции) — можно удалить
- **M2** v0.8.0 миграция ре-ранается каждый стартап
- **M3** N+1: `get_posters_for_titles` (898-915)
- **M4** N+1: `g_get_all_santa_assignments` (2371-2412)
- **M5** `g_assign_santas` — короткие циклы (n=4, shift=2 даёт два 2-цикла)
- **M6** `g_mark_santa_gift_sent` — несогласованные UPDATE
- **M7** LIKE-поиск case-sensitive для кириллицы
- **M8** N INSERT в цикле без `executemany`

### web.py (2710 строк)
- **C1** Multi-tenant изоляция сломана (199-221)
- **C2** `is_admin` глобальный (81-86)
- **C3** Brute-force на /login (231-383)
- **C4** WebSocket без auth + утечка `added_by` (2680-2710)
- **H1** Session cookie кеширует права на 7 дней (34-48)
- **H2** `/api/quotes` — массовая имперсонификация через `author` (1308-1345)
- **H3** `/api/watched/{id}/edit` — любой юзер может переименовать чужую запись (1003-1022)
- **H4** `/logout` через GET (386-391)
- **H5** `/api/users/{id}/reset-password` — глобальный (451-491)
- **H6** `/set-password` без подтверждения старого пароля + мин. длина 4 (412-448)
- **M1** IDOR: `winner_id` из URL без проверки принадлежности к guild
- **M2** Race conditions (дублирует db.py)
- **M3** `/api/wheel/spin` и `/api/collection/start_spin` не идемпотентны
- **M4** Information disclosure: `str(e)` в error messages (3 места)
- **M5** Cookie без `secure=True`
- **M6** `/api/collection/start` — любой юзер может запустить
- **M7** N+1 в `/api/collection/status` (2284-2311)
- **M8** Dashboard: raw SQL + `list_users` без guild-scope

### bot.py + guild.py + config.py + main.py + ws_manager.py + crypto.py
- **C1** `config.py:101` — небезопасный дефолт пароля `changeme`
- **C2** `config.py:66` — Path traversal в `DATABASE_PATH` валидации
- **C3** `bot.py:1314` — TG link-код 6 цифр, брутфорсится за 10 мин TTL
- **C4** `bot.py:293` — `_db_backup_loop` запускается при каждом on_ready (N лупов после N reconnect'ов)
- **C5** `bot.py:336-354` — `on_message` удаляет оригинал до постинга embed
- **C6** `guild.py:327-352` — TOCTOU race в `upsert_guild`
- **C7** `guild.py:437-489` — миграция не транзакционная
- **C8** `main.py:32-47` — нет graceful shutdown
- **C9** `bot.py:791` — multi-tenant утечка в `notify_wheel_winner`
- **H1-H11** См. подробный отчёт (is_guild_member обходит все guilds, on_raw_reaction_add грузит 500 цитат, asyncio.create_task без сохранения ссылки, нет connection pool, ws без auth/limit, broadcast блокируется на зависших сокетах, и т.д.)

### templates/ + style.css
- **C1** XSS в `onsubmit="confirm('...{{ user_var }}...')"` (4 шаблона)
- **H1** DOM-based XSS в `movienight.html:632-650` — `refreshParticipants()` через template-литералы
- **H2** 15 POST-форм без CSRF-токена
- **H3** Open-redirect/tabnabbing через `target="_blank"` без `rel="noopener"` (4 места)
- **H4** `quotes.html:243` — `message_link` без валидации схемы (`javascript:` возможен)
- **M1** URL-инъекция в пагинации (`_pagination.html:7`)
- **M2-M12** A11y: нет `:focus-visible`, нет `prefers-reduced-motion`, нет `prefers-color-scheme`, нет print-стилей, нет `aria-label` на icon-кнопках
- **L1** `nav.top` ~120 строк мёртвого CSS
- **L2** 14 `transition: all` (антипаттерн)
- **L3** Дублирование inline `<style>` между страницами

---

## Что сделано хорошо

1. **SQL-инъекции через имена таблиц — отсутствуют.** `guild_table()` валидирует `guild_id` и `base` через whitelist `GUILD_TABLES`. Значения всегда через `?` placeholders.
2. **`async with _connect()` — везде корректно.** Утечек соединений нет.
3. **Jinja2 autoescape — включён по умолчанию.** Ни одного `|safe` фильтра во всех 20 шаблонах.
4. **`secrets.compare_digest` + `secrets.token_hex`** — правильные крипто-примитивы (но применены к слабому KDF).
5. **Fernet (AES-128-CBC + HMAC-SHA256)** для шифрования секретов — правильный выбор.
6. **`@dataclass(frozen=True)` для Settings** — иммутабельный конфиг.
7. **Идемпотентные миграции** через `PRAGMA table_info` + `CREATE ... IF NOT EXISTS`.
8. **WebSocket переподключение с backoff** + polling fallback.
9. **Glassmorphism design system** — консистентная палитра, две согласованные темы.
10. **Логирование** — пароли/токены не логируются.

---

## План исправления (приоритизированный)

### Фаза 1 — Срочно (безопасность, можно сделать в отдельных ветках)

1. **Multi-tenant изоляция**: `bot.is_guild_member(discord_id, guild_id)` + проверка в `get_user_guilds`
2. **Per-guild admin**: миграция `users.is_admin` → `guild_members.is_admin`, в `require_admin` per-guild проверка
3. **WebSocket auth**: валидация session cookie в handshake + origin whitelist + анонимизация broadcast
4. **PBKDF2/argon2id**: заменить `_hash_password`, мин. длина 8, при следующем логине пере-хешировать
5. **Rate-limiting на /login**: slowapi, 5 попыток / 10 мин
6. **CSRF-токены**: fastapi-csrf-protect во всех POST-формах
7. **XSS в confirm()**: `data-confirm-text` + JS-слушатель вместо интерполяции в JS-строки

### Фаза 2 — Скоро (race conditions, целостность данных)

1. `BEGIN IMMEDIATE` в `g_get_or_create_winner_by_title`, `g_start_collection`, `g_complete_collection_spin`, `verify_tg_link`, `g_add_collection_participant`, `g_add_santa_participant`, `g_add_to_watchlist`
2. UNIQUE-индексы на `lower(title)` (watched), `lower(lot_name)` (winners), `link_code` (tg_links)
3. `g_list_watched` — убрать LEFT JOIN, скалярный подзапрос для `winner_id` (детерминированно)
4. Timezone: `datetime.now(timezone.utc)` вместо `datetime.utcnow()`
5. Идемпотентность: `Idempotency-Key` для `/api/wheel/spin` и `/api/collection/start_spin`

### Фаза 3 — В плановом порядке (производительность, UX, a11y)

1. Удалить ~900 строк мёртвого кода в db.py (legacy non-g_ функции)
2. N+1: `get_posters_for_titles`, `g_get_all_santa_assignments`, `/api/collection/status`, dashboard
3. `executemany` для батчей (collection_picks, santas, wheel_items)
4. A11y: `:focus-visible`, `prefers-reduced-motion`, `prefers-color-scheme`, `aria-label`, print-стили
5. Удалить мёртвый CSS (`nav.top`, дублирующие `article.card`)
6. Cookie `secure=True`
7. Graceful shutdown: signal handlers, `bot.close()`, отмена фоновых тасок
8. Connection pool для SQLite
9. Schema versioning для миграций
