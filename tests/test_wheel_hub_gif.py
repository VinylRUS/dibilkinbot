"""v3.8.0: Тесты центральной GIF колеса (хаб).

Покрывает:
- GET /api/wheel/hub_gif — возвращает текущий URL (require_user)
- POST /api/wheel/hub_gif — устанавливает URL (require_superuser)
  - валидация URL (http/https only — анти-SSRF)
  - пустой URL → очищает (возвращает пчелу 🐝)
  - URL > 2048 chars → 400
- db.g_get_hub_gif / g_set_hub_gif — хранение в guild_settings
- ws_manager.broadcast_hub_gif_changed — broadcast идёт всем WS-клиентам гильдии
- Синхронизация между клиентами (через WS broadcast)

Использует conftest client fixture (TEST_GUILD_ID=1515661305011376209, is_superuser=True).
"""
from __future__ import annotations

import pytest


# ============ db.g_get_hub_gif / g_set_hub_gif ============

def test_hub_gif_default_is_none(client):
    """По умолчанию hub_gif не установлен — возвращает None (пчела 🐝)."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    async def _get():
        return await db.g_get_hub_gif(TEST_GID)
    url = asyncio.run(_get())
    assert url is None


def test_hub_gif_set_and_get(client):
    """Установка URL → последующее чтение возвращает тот же URL."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    test_url = "https://media.giphy.com/media/abc123/giphy.gif"
    async def _set_get():
        await db.g_set_hub_gif(TEST_GID, test_url)
        return await db.g_get_hub_gif(TEST_GID)
    url = asyncio.run(_set_get())
    assert url == test_url


def test_hub_gif_clear_with_none(client):
    """Передача None в g_set_hub_gif очищает (возвращает None)."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    async def _set_clear_get():
        await db.g_set_hub_gif(TEST_GID, "https://example.com/x.gif")
        await db.g_set_hub_gif(TEST_GID, None)
        return await db.g_get_hub_gif(TEST_GID)
    url = asyncio.run(_set_clear_get())
    assert url is None


def test_hub_gif_clear_with_empty_string(client):
    """Пустая строка → g_set_hub_gif strip'ит до пустой → None → DELETE."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    async def _set_clear_get():
        await db.g_set_hub_gif(TEST_GID, "https://example.com/x.gif")
        await db.g_set_hub_gif(TEST_GID, "   ")  # whitespace-only
        return await db.g_get_hub_gif(TEST_GID)
    url = asyncio.run(_set_clear_get())
    assert url is None


def test_hub_gif_strips_whitespace(client):
    """URL с surrounding whitespace → strip'ится перед сохранением."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    async def _set_get():
        await db.g_set_hub_gif(TEST_GID, "  https://example.com/x.gif  ")
        return await db.g_get_hub_gif(TEST_GID)
    url = asyncio.run(_set_get())
    assert url == "https://example.com/x.gif"


# ============ API: GET /api/wheel/hub_gif ============

def test_api_get_hub_gif_returns_url(client):
    """GET /api/wheel/hub_gif возвращает текущий URL (после установки через db)."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    test_url = "https://media.tenor.com/abc/xyz.gif"
    asyncio.run(db.g_set_hub_gif(TEST_GID, test_url))
    r = client.get("/api/wheel/hub_gif")
    assert r.status_code == 200
    assert r.json()["url"] == test_url


def test_api_get_hub_gif_default_none(client):
    """GET /api/wheel/hub_gif без установленного URL → {"url": null}."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_hub_gif(TEST_GID, None))
    r = client.get("/api/wheel/hub_gif")
    assert r.status_code == 200
    assert r.json()["url"] is None


# ============ API: POST /api/wheel/hub_gif ============

def test_api_set_hub_gif_success(client):
    """POST с валидным https URL → 200, DB обновлена."""
    test_url = "https://media.giphy.com/media/abc/giphy.gif"
    r = client.post("/api/wheel/hub_gif", data={"url": test_url})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["url"] == test_url
    # DB обновлена
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    url = asyncio.run(db.g_get_hub_gif(TEST_GID))
    assert url == test_url


def test_api_set_hub_gif_http_scheme(client):
    """POST с http:// URL (не https) → тоже валидно."""
    r = client.post("/api/wheel/hub_gif", data={"url": "http://example.com/x.gif"})
    assert r.status_code == 200
    assert r.json()["url"] == "http://example.com/x.gif"


def test_api_set_hub_gif_empty_clears(client):
    """POST с пустым URL → 200, DB очищена (None)."""
    # Сначала установим
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_hub_gif(TEST_GID, "https://example.com/x.gif"))
    # Теперь очищаем
    r = client.post("/api/wheel/hub_gif", data={"url": ""})
    assert r.status_code == 200
    assert r.json()["url"] is None
    # DB действительно пустая
    url = asyncio.run(db.g_get_hub_gif(TEST_GID))
    assert url is None


def test_api_set_hub_gif_rejects_javascript_scheme(client):
    """POST с javascript: URL → 400 (анти-XSS)."""
    r = client.post("/api/wheel/hub_gif", data={"url": "javascript:alert(1)"})
    assert r.status_code == 400
    assert "scheme" in r.json()["error"].lower() or "http" in r.json()["error"].lower()


def test_api_set_hub_gif_rejects_data_scheme(client):
    """POST с data: URL → 400."""
    r = client.post("/api/wheel/hub_gif", data={"url": "data:image/gif;base64,R0lGODlh"})
    assert r.status_code == 400


def test_api_set_hub_gif_rejects_missing_host(client):
    """POST с https:///path (без host) → 400."""
    r = client.post("/api/wheel/hub_gif", data={"url": "https:///no-host.gif"})
    assert r.status_code == 400
    assert "host" in r.json()["error"].lower()


def test_api_set_hub_gif_rejects_too_long(client):
    """POST с URL > 2048 chars → 400."""
    long_url = "https://example.com/" + "a" * 2100
    r = client.post("/api/wheel/hub_gif", data={"url": long_url})
    assert r.status_code == 400
    assert "long" in r.json()["error"].lower()


# ============ ws_manager.broadcast_hub_gif_changed ============

def test_broadcast_hub_gif_changed_calls_broadcast_guild(monkeypatch):
    """broadcast_hub_gif_changed делегирует в broadcast_guild с правильным event_type + payload."""
    import asyncio
    import ws_manager

    captured = []

    async def fake_broadcast_guild(guild_id, event_type, payload):
        captured.append((guild_id, event_type, payload))

    monkeypatch.setattr(ws_manager, "broadcast_guild", fake_broadcast_guild)

    asyncio.run(ws_manager.broadcast_hub_gif_changed(123, "https://example.com/hub.gif"))
    assert len(captured) == 1
    guild_id, event_type, payload = captured[0]
    assert guild_id == 123
    assert event_type == "hub_gif_changed"
    assert payload == {"url": "https://example.com/hub.gif"}


def test_broadcast_hub_gif_changed_with_none(monkeypatch):
    """broadcast с None URL → payload {"url": None} (клиенты вернутся к пчеле 🐝)."""
    import asyncio
    import ws_manager

    captured = []

    async def fake_broadcast_guild(guild_id, event_type, payload):
        captured.append((guild_id, event_type, payload))

    monkeypatch.setattr(ws_manager, "broadcast_guild", fake_broadcast_guild)

    asyncio.run(ws_manager.broadcast_hub_gif_changed(456, None))
    assert captured == [(456, "hub_gif_changed", {"url": None})]


# ============ Integration: POST → broadcast fired ============

def test_post_hub_gif_triggers_broadcast(monkeypatch, client):
    """POST /api/wheel/hub_gif → вызывает ws_manager.broadcast_hub_gif_changed.

    Интеграционный тест: endpoint вызывает broadcast с тем же URL что записал в БД.
    Синхронизация через WS реально работает.
    """
    import ws_manager

    broadcast_calls = []

    async def fake_broadcast(guild_id, url):
        broadcast_calls.append((guild_id, url))

    # Подменяем broadcast_hub_gif_changed (не broadcast_guild — тестируем именно обёртку)
    monkeypatch.setattr(ws_manager, "broadcast_hub_gif_changed", fake_broadcast)
    # Также monkeypatch в wheel_routes который уже импортировал ws_manager
    import wheel_routes
    monkeypatch.setattr(wheel_routes.ws_manager, "broadcast_hub_gif_changed", fake_broadcast)

    test_url = "https://media.giphy.com/media/test_broadcast/giphy.gif"
    r = client.post("/api/wheel/hub_gif", data={"url": test_url})
    assert r.status_code == 200

    assert len(broadcast_calls) == 1, f"expected 1 broadcast call, got {len(broadcast_calls)}"
    guild_id, broadcasted_url = broadcast_calls[0]
    assert broadcasted_url == test_url
    # guild_id должен быть int (TEST_GUILD_ID из conftest)
    assert guild_id == 1515661305011376209


def test_post_hub_gif_clear_triggers_broadcast_with_none(monkeypatch, client):
    """POST с пустым URL → broadcast с None (клиенты вернутся к пчеле 🐝)."""
    import wheel_routes
    import ws_manager

    broadcast_calls = []
    async def fake_broadcast(guild_id, url):
        broadcast_calls.append((guild_id, url))
    monkeypatch.setattr(ws_manager, "broadcast_hub_gif_changed", fake_broadcast)
    monkeypatch.setattr(wheel_routes.ws_manager, "broadcast_hub_gif_changed", fake_broadcast)

    r = client.post("/api/wheel/hub_gif", data={"url": ""})
    assert r.status_code == 200
    assert len(broadcast_calls) == 1
    guild_id, broadcasted_url = broadcast_calls[0]
    assert broadcasted_url is None


# ============ v3.8.1: Библиотека гифок ============

def test_library_default_empty(client):
    """По умолчанию библиотека пустая."""
    r = client.get("/api/wheel/hub_gif/library")
    assert r.status_code == 200
    assert r.json()["urls"] == []


def test_library_add_via_post_endpoint(client):
    """POST /api/wheel/hub_gif/library добавляет URL, возвращает обновлённый список."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_hub_gif(TEST_GID, None))  # сброс
    # также сбросим library
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))

    r = client.post("/api/wheel/hub_gif/library", data={"url": "https://media.giphy.com/media/abc/giphy.gif"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "https://media.giphy.com/media/abc/giphy.gif" in r.json()["urls"]


def test_library_add_dedup(client):
    """Добавление того же URL — no-op, не дублирует."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))
    url = "https://media.tenor.com/x.gif"

    r1 = client.post("/api/wheel/hub_gif/library", data={"url": url})
    assert r1.status_code == 200
    r2 = client.post("/api/wheel/hub_gif/library", data={"url": url})
    assert r2.status_code == 200
    assert r2.json()["urls"].count(url) == 1, f"duplicate in library: {r2.json()['urls']}"


def test_library_remove(client):
    """DELETE /api/wheel/hub_gif/library удаляет URL."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))
    url1 = "https://example.com/a.gif"
    url2 = "https://example.com/b.gif"
    client.post("/api/wheel/hub_gif/library", data={"url": url1})
    client.post("/api/wheel/hub_gif/library", data={"url": url2})

    r = client.delete("/api/wheel/hub_gif/library", params={"url": url1})
    assert r.status_code == 200
    urls = r.json()["urls"]
    assert url1 not in urls
    assert url2 in urls


def test_library_remove_nonexistent_noop(client):
    """DELETE несуществующего URL — no-op, не падает."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))
    r = client.delete("/api/wheel/hub_gif/library", params={"url": "https://nonexistent.example.com/x.gif"})
    assert r.status_code == 200
    assert r.json()["urls"] == []


def test_library_clear_when_last_removed(client):
    """Удаление последнего URL → ключ удаляется из settings (None)."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))
    url = "https://example.com/last.gif"
    client.post("/api/wheel/hub_gif/library", data={"url": url})
    client.delete("/api/wheel/hub_gif/library", params={"url": url})
    # Проверим что ключ удалён из БД
    raw = asyncio.run(db.g_get_setting(TEST_GID, "hub_gif_library"))
    assert raw is None, f"library key should be deleted when empty, got {raw!r}"


def test_post_hub_gif_with_save_to_library(client):
    """POST /api/wheel/hub_gif с save_to_library=true → устанавливает гифку И сохраняет в библиотеку."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_hub_gif(TEST_GID, None))
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))

    url = "https://media.giphy.com/media/save_test/giphy.gif"
    r = client.post("/api/wheel/hub_gif", data={"url": url, "save_to_library": "true"})
    assert r.status_code == 200
    assert r.json()["url"] == url
    assert "library" in r.json(), "response should include library when save_to_library=true"
    assert url in r.json()["library"]

    # БД — hub_gif установлен
    assert asyncio.run(db.g_get_hub_gif(TEST_GID)) == url
    # БД — library содержит URL
    assert url in asyncio.run(db.g_get_hub_gif_library(TEST_GID))


def test_post_hub_gif_without_save_to_library(client):
    """POST /api/wheel/hub_gif без save_to_library → устанавливает, но НЕ сохраняет в библиотеку."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_hub_gif(TEST_GID, None))
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))

    url = "https://media.giphy.com/media/no_save/giphy.gif"
    r = client.post("/api/wheel/hub_gif", data={"url": url})
    assert r.status_code == 200
    assert r.json()["url"] == url
    assert "library" not in r.json(), "response should NOT include library when save_to_library not set"
    assert asyncio.run(db.g_get_hub_gif(TEST_GID)) == url
    assert asyncio.run(db.g_get_hub_gif_library(TEST_GID)) == [], "library should remain empty"


def test_post_hub_gif_clear_does_not_touch_library(client):
    """POST /api/wheel/hub_gif с пустым URL → очищает hub_gif, но НЕ трогает библиотеку."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    url = "https://example.com/persistent.gif"
    # Сначала наполним библиотеку
    asyncio.run(db.g_add_hub_gif_to_library(TEST_GID, url))
    # И установим активную гифку
    asyncio.run(db.g_set_hub_gif(TEST_GID, "https://example.com/active.gif"))

    r = client.post("/api/wheel/hub_gif", data={"url": ""})
    assert r.status_code == 200
    assert r.json()["url"] is None
    # hub_gif очищен
    assert asyncio.run(db.g_get_hub_gif(TEST_GID)) is None
    # library не тронута
    assert url in asyncio.run(db.g_get_hub_gif_library(TEST_GID))


def test_library_rejects_invalid_url_scheme(client):
    """POST /api/wheel/hub_gif/library с javascript: → 400."""
    r = client.post("/api/wheel/hub_gif/library", data={"url": "javascript:alert(1)"})
    assert r.status_code == 400


def test_library_rejects_missing_url(client):
    """POST /api/wheel/hub_gif/library без url в форме → 422 (Form required)."""
    r = client.post("/api/wheel/hub_gif/library", data={})
    assert r.status_code == 422  # FastAPI Form(...) → 422 если параметр обязателен и не передан


def test_db_library_json_corruption_returns_empty(client):
    """Если JSON в guild_settings повреждён — g_get_hub_gif_library возвращает пустой list, не падает."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", "not-a-valid-json"))
    urls = asyncio.run(db.g_get_hub_gif_library(TEST_GID))
    assert urls == [], "corrupt JSON should yield empty list"


def test_db_library_strips_whitespace_in_urls(client):
    """URL с whitespace — strip'ится перед сохранением в библиотеку."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))
    asyncio.run(db.g_add_hub_gif_to_library(TEST_GID, "  https://example.com/spaced.gif  "))
    urls = asyncio.run(db.g_get_hub_gif_library(TEST_GID))
    assert "https://example.com/spaced.gif" in urls
    assert "  https://example.com/spaced.gif  " not in urls


# ============ v3.8.3: Разделение прав — обычный юзер ============

def _make_user_session(is_superuser: bool):
    """Создать session token для юзера с заданной ролью."""
    import deps
    return deps.create_session({
        "discord_id": 9999 if not is_superuser else 1001,
        "username": "regular" if not is_superuser else "matka",
        "display_name": "Regular" if not is_superuser else "Matka",
        "is_admin": is_superuser,
        "is_superuser": is_superuser,
        "role": "superuser" if is_superuser else "user",
        "current_guild_id": 1515661305011376209,
    })


def test_regular_user_can_set_hub_gif_without_save(client):
    """v3.8.3: обычный юзер (не Матка) может установить гифку через POST без save_to_library."""
    # Подменяем cookie на сессию обычного юзера
    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_hub_gif(TEST_GID, None))

    url = "https://media.giphy.com/media/regular_user_test/giphy.gif"
    r = client.post("/api/wheel/hub_gif", data={"url": url})
    assert r.status_code == 200, f"regular user should be able to set hub_gif: {r.text}"
    assert r.json()["url"] == url
    # DB обновлена
    assert asyncio.run(db.g_get_hub_gif(TEST_GID)) == url


def test_regular_user_cannot_save_to_library(client):
    """v3.8.3: обычный юзер с save_to_library=True → 403 (только Матка может сохранять)."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    # Очистим БД от возможных артефактов предыдущих тестов
    asyncio.run(db.g_set_hub_gif(TEST_GID, None))

    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    url = "https://media.giphy.com/media/save_attempt/giphy.gif"
    r = client.post("/api/wheel/hub_gif", data={"url": url, "save_to_library": "true"})
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "superuser" in r.json()["error"].lower() or "Матка" in r.json()["error"]
    # Гифка НЕ установлена (запрос отклонён до записи в БД)
    assert asyncio.run(db.g_get_hub_gif(TEST_GID)) is None
    # Библиотека НЕ touched
    assert "https://media.giphy.com/media/save_attempt/giphy.gif" not in asyncio.run(db.g_get_hub_gif_library(TEST_GID))


def test_regular_user_cannot_clear_hub_gif_via_save(client):
    """v3.8.3: обычный юзер с save_to_library=True + пустой URL → 403 (хоть и пустой url,
    save_to_library всё равно требует superuser — guard срабатывает до проверки url)."""
    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    r = client.post("/api/wheel/hub_gif", data={"url": "", "save_to_library": "true"})
    assert r.status_code == 403


def test_regular_user_can_clear_hub_gif_without_save(client):
    """v3.8.3: обычный юзер может убрать гифку (POST с пустым URL, без save_to_library)."""
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    # Сначала установим (от лица Матки — через db напрямую)
    asyncio.run(db.g_set_hub_gif(TEST_GID, "https://example.com/existing.gif"))

    # Теперь обычный юзер убирает
    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    r = client.post("/api/wheel/hub_gif", data={"url": ""})
    assert r.status_code == 200
    assert r.json()["url"] is None
    assert asyncio.run(db.g_get_hub_gif(TEST_GID)) is None


def test_regular_user_cannot_add_to_library(client):
    """v3.8.3: обычный юзер не может POST /api/wheel/hub_gif/library (require_superuser)."""
    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    r = client.post("/api/wheel/hub_gif/library", data={"url": "https://example.com/x.gif"})
    assert r.status_code == 403


def test_regular_user_cannot_remove_from_library(client):
    """v3.8.3: обычный юзер не может DELETE /api/wheel/hub_gif/library (require_superuser)."""
    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    r = client.delete("/api/wheel/hub_gif/library", params={"url": "https://example.com/x.gif"})
    assert r.status_code == 403


def test_regular_user_can_get_library(client):
    """v3.8.3: обычный юзер может GET /api/wheel/hub_gif/library (require_user — видят все)."""
    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    r = client.get("/api/wheel/hub_gif/library")
    assert r.status_code == 200
    assert "urls" in r.json()


def test_regular_user_can_get_hub_gif(client):
    """v3.8.3: обычный юзер может GET /api/wheel/hub_gif (require_user — видят все)."""
    token = _make_user_session(is_superuser=False)
    client.cookies.set("session", token)

    r = client.get("/api/wheel/hub_gif")
    assert r.status_code == 200
    assert "url" in r.json()


def test_matka_can_still_save_to_library(client):
    """v3.8.3: Матка по-прежнему может save_to_library=True (регрессия — guard не сломал Матку)."""
    # Явно установим matka-сессию (могли остаться cookies от предыдущих тестов)
    token_matka = _make_user_session(is_superuser=True)
    client.cookies.set("session", token_matka)
    import asyncio
    import db
    TEST_GID = 1515661305011376209
    asyncio.run(db.g_set_setting(TEST_GID, "hub_gif_library", None))

    url = "https://media.giphy.com/media/matka_still_works/giphy.gif"
    r = client.post("/api/wheel/hub_gif", data={"url": url, "save_to_library": "true"})
    assert r.status_code == 200
    assert url in r.json().get("library", [])
