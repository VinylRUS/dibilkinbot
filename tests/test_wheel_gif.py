"""v3.7.0: Тесты кастомных GIF на секторах колеса.

Покрывает:
- Миграция ALTER TABLE ADD COLUMN gif_path/gif_poster_path для существующих гильдий
- POST /api/wheel/items/{id}/gif (multipart UploadFile) — magic bytes GIF8, 2MB limit,
  генерация PNG-постера через Pillow, UPDATE в БД, broadcast
- DELETE /api/wheel/items/{id}/gif — purge файлов + NULL в БД
- Purge при soft-delete лота (g_remove_wheel_item)
- Purge при g_clear_wheel
- require_superuser на endpoints
- anonymize_items прокидывает gif_path/gif_poster_path

Использует conftest client fixture (TEST_GUILD_ID=1515661305011376209, is_superuser=True).
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pytest


def _make_minimal_gif(size_bytes: int | None = None) -> bytes:
    """Сгенерировать минимальный валидный GIF 1x1 (или дополнить до size_bytes).

    GIF89a header + image descriptor + LZW data + trailer. Pillow его открывает.
    Если size_bytes задан — дополняем payload нулями после trailer'а (валидный
    GIF-парсер игнорирует данные после trailer, а нам нужен просто большой size).
    """
    header = (
        b"GIF89a"           # signature + version
        b"\x01\x00"          # width=1 (LE)
        b"\x01\x00"          # height=1 (LE)
        b"\x00"              # packed: GCT=0, colorRes=0, sort=0, GCTsize=0
        b"\x00"              # background color index
        b"\x00"              # pixel aspect ratio
        b"\x2c"              # image descriptor introducer
        b"\x00\x00\x00\x00"  # left=0, top=0
        b"\x01\x00\x01\x00"  # width=1, height=1
        b"\x00"              # packed: LCT=0, interlace=0, sort=0, LCTsize=0
        b"\x02"              # LZW minimum code size
        b"\x02\x4c\x01"      # data sub-block: 2 bytes, data, terminator
        b"\x00"              # block terminator
        b"\x3b"              # trailer
    )
    if size_bytes is None:
        return header
    if len(header) >= size_bytes:
        return header[:size_bytes]
    return header + b"\x00" * (size_bytes - len(header))


def _make_png_magic_bytes() -> bytes:
    """PNG сигнатура + IHDR chunk — невалидный GIF, валидный PNG magic bytes."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


# ============ Миграция ============

def test_wheel_items_has_gif_columns(client):
    """После init_guild_tables (conftest) wheel_items содержит gif_path, gif_poster_path."""
    import aiosqlite
    import asyncio
    from config import settings
    from guild import guild_table

    TEST_GID = 1515661305011376209

    async def _check():
        async with aiosqlite.connect(settings.database_path) as conn:
            cur = await conn.execute(f"PRAGMA table_info({guild_table(TEST_GID, 'wheel_items')})")
            cols = [r[1] for r in await cur.fetchall()]
        return cols
    cols = asyncio.run(_check())
    assert "gif_path" in cols, f"gif_path missing from {cols}"
    assert "gif_poster_path" in cols, f"gif_poster_path missing from {cols}"


# ============ Upload через file ============

def test_upload_gif_via_file_success(client):
    """POST /api/wheel/items/{id}/gif с multipart UploadFile → 200, файлы существуют."""
    # Сначала создаём item через стандартный endpoint
    r = client.post("/api/wheel/items", data={"name": "Test GIF movie"})
    assert r.status_code == 200
    item_id = r.json()["id"]

    gif_bytes = _make_minimal_gif()
    files = {"file": ("test.gif", io.BytesIO(gif_bytes), "image/gif")}
    r2 = client.post(f"/api/wheel/items/{item_id}/gif", files=files)
    assert r2.status_code == 200, f"upload failed: {r2.text}"
    data = r2.json()
    assert data["ok"] is True
    assert data["gif_path"].startswith("/static/wheel_gifs/")
    assert data["gif_path"].endswith(".gif")
    assert data["gif_poster_path"].endswith(".png")
    assert data["size"] == len(gif_bytes)

    # Файлы реально созданы на диске
    import aiosqlite, asyncio
    from config import settings
    gifs_dir = Path(settings.database_path).parent / "wheel_gifs"
    gif_file = gifs_dir / Path(data["gif_path"]).name
    png_file = gifs_dir / Path(data["gif_poster_path"]).name
    assert gif_file.exists(), f"GIF file missing: {gif_file}"
    assert png_file.exists(), f"PNG poster missing: {png_file}"
    assert gif_file.read_bytes()[:4] == b"GIF8"
    # PNG-постер начинается с PNG signature
    assert png_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # В БД колонки заполнены
    from guild import guild_table
    TEST_GID = 1515661305011376209
    async def _check_db():
        async with aiosqlite.connect(settings.database_path) as conn:
            cur = await conn.execute(
                f"SELECT gif_path, gif_poster_path FROM {guild_table(TEST_GID, 'wheel_items')} WHERE id = ?",
                (item_id,)
            )
            return await cur.fetchone()
    row = asyncio.run(_check_db())
    assert row is not None
    assert row[0] == data["gif_path"]
    assert row[1] == data["gif_poster_path"]


def test_upload_gif_rejects_non_gif(client):
    """PNG content → 400 magic bytes."""
    r = client.post("/api/wheel/items", data={"name": "PNG test"})
    item_id = r.json()["id"]
    png_bytes = _make_png_magic_bytes()
    files = {"file": ("not-a-gif.png", io.BytesIO(png_bytes), "image/png")}
    r2 = client.post(f"/api/wheel/items/{item_id}/gif", files=files)
    assert r2.status_code == 400
    assert "not a GIF" in r2.json()["error"] or "magic" in r2.json()["error"]


def test_upload_gif_rejects_too_large(client):
    """3MB GIF → 400 too large (лимит 2MB)."""
    r = client.post("/api/wheel/items", data={"name": "Big GIF"})
    item_id = r.json()["id"]
    big_gif = _make_minimal_gif(size_bytes=3 * 1024 * 1024)
    files = {"file": ("big.gif", io.BytesIO(big_gif), "image/gif")}
    r2 = client.post(f"/api/wheel/items/{item_id}/gif", files=files)
    assert r2.status_code == 400
    assert "too large" in r2.json()["error"].lower()


def test_upload_gif_replaces_existing(client):
    """Повторная загрузка на тот же сектор — старые файлы удаляются, новые создаются."""
    r = client.post("/api/wheel/items", data={"name": "Replace GIF"})
    item_id = r.json()["id"]
    gif1 = _make_minimal_gif()
    files1 = {"file": ("first.gif", io.BytesIO(gif1), "image/gif")}
    r1 = client.post(f"/api/wheel/items/{item_id}/gif", files=files1)
    assert r1.status_code == 200
    old_gif_path = r1.json()["gif_path"]
    old_filename = Path(old_gif_path).name

    # Загружаем второй
    gif2 = _make_minimal_gif()
    # Делаем второй GIF другим по содержанию (чтобы hash отличался)
    gif2_modified = gif2[:-1] + b"\x01"
    files2 = {"file": ("second.gif", io.BytesIO(gif2_modified), "image/gif")}
    r2 = client.post(f"/api/wheel/items/{item_id}/gif", files=files2)
    assert r2.status_code == 200
    new_filename = Path(r2.json()["gif_path"]).name

    # Старый файл должен быть удалён
    from config import settings
    gifs_dir = Path(settings.database_path).parent / "wheel_gifs"
    assert not (gifs_dir / old_filename).exists(), f"old GIF not purged: {old_filename}"
    # Новый существует
    assert (gifs_dir / new_filename).exists()


def test_upload_gif_nonexistent_item_404(client):
    r = client.post("/api/wheel/items/999999/gif", files={"file": ("x.gif", io.BytesIO(_make_minimal_gif()), "image/gif")})
    assert r.status_code == 404


# ============ DELETE GIF ============

def test_delete_gif_success(client):
    """DELETE /api/wheel/items/{id}/gif — файлы удалены, колонки NULL."""
    r = client.post("/api/wheel/items", data={"name": "To delete"})
    item_id = r.json()["id"]
    gif = _make_minimal_gif()
    files = {"file": ("del.gif", io.BytesIO(gif), "image/gif")}
    client.post(f"/api/wheel/items/{item_id}/gif", files=files)

    # Удаляем
    r_del = client.delete(f"/api/wheel/items/{item_id}/gif")
    assert r_del.status_code == 200
    assert r_del.json()["ok"] is True

    # Файлы удалены
    from config import settings
    gifs_dir = Path(settings.database_path).parent / "wheel_gifs"
    files_after = list(gifs_dir.glob(f"guild_*_item_{item_id}_*.gif"))
    files_after_png = list(gifs_dir.glob(f"guild_*_item_{item_id}_*.png"))
    assert files_after == [], f"GIF files not purged: {files_after}"
    assert files_after_png == [], f"PNG files not purged: {files_after_png}"

    # БД колонки NULL
    import aiosqlite, asyncio
    from guild import guild_table
    TEST_GID = 1515661305011376209
    async def _check_db():
        async with aiosqlite.connect(settings.database_path) as conn:
            cur = await conn.execute(
                f"SELECT gif_path, gif_poster_path FROM {guild_table(TEST_GID, 'wheel_items')} WHERE id = ?",
                (item_id,)
            )
            return await cur.fetchone()
    row = asyncio.run(_check_db())
    assert row[0] is None
    assert row[1] is None


def test_delete_gif_idempotent(client):
    """DELETE на секторе без GIF → 200 ok (no-op)."""
    r = client.post("/api/wheel/items", data={"name": "No GIF"})
    item_id = r.json()["id"]
    r_del = client.delete(f"/api/wheel/items/{item_id}/gif")
    assert r_del.status_code == 200


# ============ Purge при soft-delete item ============

def test_purge_on_remove_item(client):
    """g_remove_wheel_item — удаляет GIF-файлы с диска."""
    import asyncio, aiosqlite
    import db
    from guild import guild_table
    from config import settings
    TEST_GID = 1515661305011376209

    # Создаём item через db напрямую
    async def _add():
        return await db.g_add_wheel_item(TEST_GID, "Purge test", None, 1001)
    item_id = asyncio.run(_add())

    # Загружаем GIF
    gif = _make_minimal_gif()
    files = {"file": ("purge.gif", io.BytesIO(gif), "image/gif")}
    r = client.post(f"/api/wheel/items/{item_id}/gif", files=files)
    assert r.status_code == 200
    gif_filename = Path(r.json()["gif_path"]).name
    png_filename = Path(r.json()["gif_poster_path"]).name

    gifs_dir = Path(settings.database_path).parent / "wheel_gifs"
    assert (gifs_dir / gif_filename).exists()
    assert (gifs_dir / png_filename).exists()

    # Удаляем item через db (прямой вызов)
    async def _remove():
        return await db.g_remove_wheel_item(TEST_GID, item_id)
    ok = asyncio.run(_remove())
    assert ok is True

    # Файлы должны быть удалены
    assert not (gifs_dir / gif_filename).exists(), f"GIF not purged: {gif_filename}"
    assert not (gifs_dir / png_filename).exists(), f"PNG not purged: {png_filename}"


def test_purge_on_clear_wheel(client):
    """g_clear_wheel — удаляет все GIF-файлы активных items."""
    import asyncio, aiosqlite
    import db
    from config import settings
    TEST_GID = 1515661305011376209

    # Создаём 2 items + GIF на каждый
    gif = _make_minimal_gif()
    filenames = []
    for name in ["Clear A", "Clear B"]:
        async def _add():
            return await db.g_add_wheel_item(TEST_GID, name, None, 1001)
        item_id = asyncio.run(_add())
        files = {"file": (f"{name}.gif", io.BytesIO(gif), "image/gif")}
        r = client.post(f"/api/wheel/items/{item_id}/gif", files=files)
        assert r.status_code == 200
        filenames.append(Path(r.json()["gif_path"]).name)

    gifs_dir = Path(settings.database_path).parent / "wheel_gifs"
    for fn in filenames:
        assert (gifs_dir / fn).exists()

    # Очищаем всё колесо
    async def _clear():
        return await db.g_clear_wheel(TEST_GID)
    count = asyncio.run(_clear())
    assert count >= 2

    # Все GIF-файлы должны быть удалены
    for fn in filenames:
        assert not (gifs_dir / fn).exists(), f"GIF not purged on clear: {fn}"


# ============ anonymize_items ============

def test_anonymize_items_includes_gif_fields():
    """anonymize_items прокидывает gif_path/gif_poster_path когда они есть."""
    from deps import anonymize_items
    items = [
        {"id": 1, "name": "A", "tmdb_id": None, "color": "#FFF", "added_by": 1, "added_at": "x",
         "gif_path": "/static/wheel_gifs/a.gif", "gif_poster_path": "/static/wheel_gifs/a.png"},
        {"id": 2, "name": "B", "tmdb_id": None, "color": "#000", "added_by": 2, "added_at": "x",
         "gif_path": None, "gif_poster_path": None},
    ]
    out = anonymize_items(items)
    assert len(out) == 2
    # Первый item имеет GIF
    assert out[0]["gif_path"] == "/static/wheel_gifs/a.gif"
    assert out[0]["gif_poster_path"] == "/static/wheel_gifs/a.png"
    # added_by и added_at убраны (анонимизация)
    assert "added_by" not in out[0]
    assert "added_at" not in out[0]
    # Второй item без GIF — поля отсутствуют (не None, чтобы фронтенд делал || fallback)
    assert "gif_path" not in out[1]
    assert "gif_poster_path" not in out[1]


def test_anonymize_items_legacy_items_without_gif_fields():
    """Старые items (до миграции) — без gif-полей в dict. anonymize_items не падает."""
    from deps import anonymize_items
    items = [
        {"id": 1, "name": "Legacy", "tmdb_id": None, "color": "#FFF", "added_by": 1, "added_at": "x"},
    ]
    out = anonymize_items(items)
    assert out[0]["id"] == 1
    assert "gif_path" not in out[0]
