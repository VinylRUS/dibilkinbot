# Отчёт: Аналогичный баг с потерей точности guild_id в других шаблонах

**Дата:** 2026-10-18
**Автор:** Z.ai (main)
**Заказчик:** VinylRUS
**Статус:** Только отчёт. Фикс не делался.

## Контекст

В рамках фикса колеса был найден и исправлен баг:
- `templates/wheel.html` передавал `current_guild_id` в JS как **Number** через `const GUILD_ID = {{ current_guild_id | default(0) }};`
- Discord guild_id — 18-значное число (например `1515661305011376209`), что **больше 2^53** = `9007199254740992`
- JavaScript Number (float64) теряет точность для целых > 2^53
- Результат: `1515661305011376209` → `1515661305011376000` (округлено)
- WS URL получал округлённый guild_id → сервер падал на `no such table` → бесконечный цикл реконнекта каждые 3s

Этот баг был исправлен в `templates/wheel.html` (commit `5a54fd1`, v3.0.0):
```js
// БЫЛО (баг):
const GUILD_ID = {{ current_guild_id | default(0) }};
// СТАЛО (фикс):
const GUILD_ID = "{{ current_guild_id | default(0) }}";
```

Пользователь попросил проверить другие шаблоны на аналогичную проблему — **но не фиксить пока**.

---

## Найденные проблемы

### 🔴 Критичные — сломанная функциональность + потенциальная уязвимость

#### 1. `templates/movienight.html:564` — GUILD_ID как Number (точная копия бага колеса)
```js
const GUILD_ID = {{ current_guild_id | default(0) }};
```
**Использование:** `static/js/movienight.js:18`:
```js
ws = new WebSocket(`${proto}://${location.host}/ws/wheel?guild_id=${typeof GUILD_ID !== 'undefined' ? GUILD_ID : 0}`);
```
**Симптом:** 100% совпадение с тем, что было в `wheel.html` до фикса. WS на странице `/movienight` подключается к несуществующей таблице, падает в бесконечный цикл реконнекта каждые 3s, все события сбора (`collection_participant_joined`, `collection_participant_ready`, `collection_completed`, и т.д.) молча теряются.

В v2.4.5 CHANGELOG явно написано: «movienight.js подключался к WS с захардкоженным guild_id=0... Теперь GUILD_ID передаётся из шаблона». То есть этот баг **появился в v2.4.5** — когда передали guild_id, забыли обернуть в кавычки. То же самое, что в `wheel.html`.

**Фикс:** `const GUILD_ID = "{{ current_guild_id | default(0) }}";`

---

#### 2. `templates/users.html:133` — discord_id в onclick (SECURITY)
```html
<button type="button" onclick="resetPassword({{ discord_id }})" ...>
```
**Использование:** `templates/users.html:143-146`:
```js
async function resetPassword(discordId) {
  ...
  const r = await fetch(`/api/users/${discordId}/reset-password`, { method: 'POST' });
```
**Симптом:** Admin кликает «Сбросить пароль» на юзере `171044044380700672`. Jinja выводит `resetPassword(171044044380700672)`. JS парсит литерал как Number → округляет (например до `171044044380700640`). URL `/api/users/171044044380700640/reset-password` уходит на сервер. Сервер ищет юзера с ID `171044044380700640` — **не находит**. Сброс пароля молча не происходит (если повезло) или **сбрасывается пароль ДРУГОГО юзера** (если округление совпало с чужим ID).

**Severity:** 🔴 SECURITY. Возможен несанкционированный сброс пароля произвольного юзера.

**Фикс:** `onclick="resetPassword('{{ discord_id }}')"` — кавычки + сервер должен принимать как строку.

---

#### 3. `templates/movienight.html:531` — user_discord_id в onclick (то же что #2)
```html
<button type="button" class="participant-kick" onclick="kickParticipant({{ p.user_discord_id }})">
```
**Использование:** `static/js/movienight.js:330-335`:
```js
async function kickParticipant(userId) {
  ...
  formData.append('target_user_id', userId);
  const r = await fetch('/api/collection/kick', { method: 'POST', body: formData });
```
**Симптом:** Organizer кликает «Кикнуть» на участнике. JS получает округлённый ID. FormData отправляет строку округлённого ID. Сервер пытается кикнуть несуществующего юзера (или чужого).

**Severity:** 🔴 SECURITY. Возможен кик не того участника.

**Фикс:** `onclick="kickParticipant('{{ p.user_discord_id }}')"` + `kickParticipant` должен принимать строку.

---

### 🟡 Средние — равенство работает "случайно", но display/URL — сломаны

#### 4. `templates/movienight.html:566` — USER_DISCORD_ID как Number
```js
const USER_DISCORD_ID = {{ user.discord_id or 0 }};
```
**Использование:** `static/js/movienight.js:68`:
```js
if (msg.payload.user_discord_id === USER_DISCORD_ID) {
  alert('Вас кикнули из сбора.');
  location.reload();
}
```
**Анализ:** Сервер отправляет `user_discord_id` как JSON number (`{"user_discord_id": 171044044380700672}`). JS парсит JSON → Number (округлённый). SSR `USER_DISCORD_ID` тоже округлён. Оба округлены одинаково → `===` срабатывает. **Детектор кика работает случайно правильно.**

НО:
- `String(USER_DISCORD_ID)` выводит **округлённое** число — display broken
- Если понадобится отправить `USER_DISCORD_ID` на сервер (FormData/URL) — попадёт округлённое значение

**Severity:** 🟡潜在но. Сейчас не сломано, но хрупко — любое изменение в коде может сломать.

**Фикс:** `const USER_DISCORD_ID = "{{ user.discord_id or 0 }}";` — и проверять равенство через `String(msg.payload.user_discord_id) === USER_DISCORD_ID`.

---

#### 5. `templates/movienight.html:567` — COLLECTION_STARTED_BY как Number
```js
const COLLECTION_STARTED_BY = {{ (collection.started_by if collection else 0) }};
```
**Использование:** `static/js/movienight.js:112`:
```js
const kickBtn = (isOrganizer && !isKicked && p.user_discord_id !== COLLECTION_STARTED_BY) ? ... : '';
```
**Анализ:** Та же история — оба округлены одинаково, `!==` работает случайно. Но display broken.

**Severity:** 🟡 потенциально.

**Фикс:** Тот же — строка + сравнение через `String()`.

---

### 🟢 Безопасные (проверено — Jinja выводит как строку в HTML-контексте)

Проверил все шаблоны. Эти **НЕ страдают** от бага:

| Файл | Использование | Почему безопасно |
|------|---------------|------------------|
| `users.html:94` | `href="/u/{{ discord_id }}"` | Jinja выводит int как строку в URL — без Number() парсинга |
| `users.html:111` | `<td>{{ discord_id }}</td>` | Display — текст, без Number() |
| `users.html:126` | `action="/users/{{ discord_id }}/admin"` | Form action — строка |
| `profile.html:57` | `href="/u/{{ user.discord_id }}"` | href — строка |
| `profile.html:62` | `<span>{{ user.discord_id }}</span>` | Display — строка |
| `profile.html:469` | `fetch('/u/{{ user.discord_id }}')` | Внутри строки — Jinja интерполирует как строку |
| `panel.html:116/165/215/240` | `href="/u/{{ m.discord_id }}"` | href — строка |
| `sidebar.html:55/123` | `href="/u/{{ user.discord_id }}"` | href — строка |
| `guilds.html:144/148/153` | `action="/guilds/{{ g.guild_id }}/..."` | Form action — строка |
| `admin_panel.html:296/300/305` | `action="/guilds/{{ g.guild_id }}/..."` | Form action — строка |
| `select_guild.html:127` | `value="{{ g.guild_id }}"` | Атрибут value — строка |
| `movienight.html:508` | `data-user-id="{{ p.user_discord_id }}"` | data-attr — строка |
| `modpacks.html:283` | `data-id="{{ sub.project_id }}"` | data-attr — строка |

---

## Сводка по фиксам (когда решишь делать)

| # | Файл:строка | Тип | Severity | Фикс |
|---|-------------|-----|----------|------|
| 1 | `templates/movienight.html:564` | GUILD_ID as Number | 🔴 broken | Обернуть в кавычки |
| 2 | `templates/users.html:133` | discord_id в onclick | 🔴 SECURITY | Обернуть в кавычки |
| 3 | `templates/movienight.html:531` | user_discord_id в onclick | 🔴 SECURITY | Обернуть в кавычки |
| 4 | `templates/movienight.html:566` | USER_DISCORD_ID as Number | 🟡 brittle | Обернуть + String() сравнение |
| 5 | `templates/movienight.html:567` | COLLECTION_STARTED_BY as Number | 🟡 brittle | Обернуть + String() сравнение |

## Рекомендации

1. **Сделать #1, #2, #3 СРОЧНО** — #2 и #3 это SECURITY баги (возможен кик/сброс пароля чужого юзера). #1 ломает весь `/movienight` (то же что было с `/wheel`).

2. **#4, #5 сделать заодно** — пока работают, но при ближайшем изменении кода сломаются. Лучше закрыть сразу.

3. **Серверная сторона:** проверить `/api/users/{discord_id}/reset-password` и `/api/collection/kick` — должны принимать ID как строку (или парсить как `str` затем `int`). FastAPI по умолчанию парсит path param как int — работает, но **только** если клиент шлёт точный ID. С округлённым ID — 404 или неверный юзер.

4. **Регекс для CI:** добавить линтер, который ловит `{{ [a-z_]+(discord_id|user_id|started_by|added_by|guild_id) }}` без surrounding кавычек в `<script>` блоках и `onclick`/`onload`/и т.д. атрибутах. Это не первая и не последняя находка такого рода.

5. **Документация:** добавить в `DESIGN_SYSTEM.md` или `CODEREVIEW.md` правило: «Discord IDs в JS-контексте ВСЕГДА как строки». Это 18-значные числа, > 2^53, JS Number теряет точность.
