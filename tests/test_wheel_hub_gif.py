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
