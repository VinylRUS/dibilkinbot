"""v2.4.1: Клиент Modrinth API для мониторинга модпаков.

Документация: https://docs.modrinth.com/api
Base URL: https://api.modrinth.com/v2

Используется для:
1. Получения инфы о проекте по slug/ID (название, иконка, тип)
2. Получения списка версий (для поиска новой с прошлого раза)
3. Форматирования URL скачивания вида:
   https://cdn.modrinth.com/data/<project_id>/versions/<version_id>/<filename>?mr_download_reason=standalone

Rate limit: 300 req/min per IP — для нас копейки (polling раз в 30 минут).

ВАЖНО: Modrinth требует User-Agent в запросах (иначе 403).
"""
from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("modrinth")

MODRINTH_API = "https://api.modrinth.com/v2"
USER_AGENT = "DeeBeelkin-Bot/2.4.1 (https://github.com/VinylRUS/dibilkinbot)"
REQUEST_TIMEOUT = 15.0  # seconds


class ModrinthError(Exception):
    """Базовая ошибка при работе с Modrinth API."""


class ProjectNotFoundError(ModrinthError):
    """Проект не найден (404)."""


async def get_project(project_id_or_slug: str) -> dict:
    """Получить информацию о проекте по slug или ID.

    Возвращает dict с полями:
    - id: внутренний ID проекта (например '5FFgwNNP')
    - slug: slug (например 'cobblemon-fabric')
    - title: название
    - description: короткое описание
    - icon_url: URL иконки (или None)
    - project_type: 'mod' | 'modpack' | 'resourcepack' | 'shader'
    - downloads: int
    - followers: int
    - updated: ISO-8601 дата последнего обновления
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = f"{MODRINTH_API}/project/{quote(project_id_or_slug, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 404:
            raise ProjectNotFoundError(f"Project '{project_id_or_slug}' not found")
        if r.status_code != 200:
            raise ModrinthError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    except httpx.RequestError as e:
        raise ModrinthError(f"Network error: {e}") from e


async def get_project_versions(project_id_or_slug: str, limit: int = 20) -> list[dict]:
    """Получить список версий проекта, отсортированных от свежей к старой.

    Возвращает список dict'ов с полями:
    - id: ID версии (например 'BpTjgzL8')
    - project_id: ID проекта
    - name: человекочитаемое имя версии
    - version_number: строка с номером версии
    - date_published: ISO-8601
    - version_type: 'release' | 'beta' | 'alpha'
    - changelog: markdown текст с изменениями
    - game_versions: ['1.21.1', ...]
    - loaders: ['fabric', 'forge', ...]
    - files: [{hashes, url, filename, primary, size, file_type}]
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    url = f"{MODRINTH_API}/project/{quote(project_id_or_slug, safe='')}/version"
    params = {"include_changelog": "true"}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            r = await client.get(url, headers=headers, params=params)
        if r.status_code == 404:
            raise ProjectNotFoundError(f"Project '{project_id_or_slug}' not found")
        if r.status_code != 200:
            raise ModrinthError(f"HTTP {r.status_code}: {r.text[:200]}")
        versions = r.json()
        # Modrinth по умолчанию возвращает от свежей к старой, но проверим
        versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        return versions[:limit]
    except httpx.RequestError as e:
        raise ModrinthError(f"Network error: {e}") from e


async def get_latest_version(project_id_or_slug: str) -> Optional[dict]:
    """Получить самую свежую версию проекта. None если версий нет."""
    versions = await get_project_versions(project_id_or_slug, limit=1)
    return versions[0] if versions else None


def get_primary_file(version: dict) -> Optional[dict]:
    """Найти primary файл версии (обычно .mrpack файл модпака).

    Если нет primary — берём первый. None если файлов нет.
    """
    files = version.get("files", [])
    if not files:
        return None
    for f in files:
        if f.get("primary"):
            return f
    return files[0]


def format_download_url(file: dict) -> str:
    """Сформатировать URL скачивания с mr_download_reason=standalone.

    Это нужно чтобы Modrinth корректно считал скачивание в аналитике —
    как standalone download (не через их лаунчер).
    """
    url = file.get("url", "")
    if not url:
        return ""
    # Modrinth CDN URL уже годный — просто добавляем query param
    if "?" in url:
        return f"{url}&mr_download_reason=standalone"
    return f"{url}?mr_download_reason=standalone"


def format_version_type_emoji(version_type: str) -> str:
    """Эмодзи для типа версии (release/beta/alpha)."""
    if version_type == "release":
        return "🟢"
    if version_type == "beta":
        return "🟡"
    if version_type == "alpha":
        return "🔴"
    return "⚪"


def format_filesize(size_bytes: int | None) -> str:
    """Человекочитаемый размер файла."""
    if not size_bytes:
        return "?"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def parse_project_id_from_url_or_input(text: str) -> str | None:
    """Извлечь project_id или slug из строки.

    Поддерживаемые форматы:
    - bare slug: 'cobblemon-fabric'
    - bare ID: '5FFgwNNP'
    - modrinth.com URL: 'https://modrinth.com/modpack/cobblemon-fabric'
    - modrinth.com URL с version: 'https://modrinth.com/modpack/cobblemon-fabric/version/1.8.2'
    - cdn.modrinth.com URL: 'https://cdn.modrinth.com/data/5FFgwNNP/versions/BpTjgzL8/...'

    Возвращает slug или project_id (строка). None если не удалось распарсить.
    """
    text = text.strip()
    if not text:
        return None
    # CDN URL: cdn.modrinth.com/data/<project_id>/versions/...
    # Проверяем ПЕРВЫМ, т.к. в URL тоже есть подстрока 'modrinth.com/'
    if "cdn.modrinth.com/data/" in text:
        import re
        m = re.search(r"cdn\.modrinth\.com/data/([^/]+)/versions/", text)
        if m:
            return m.group(1)
        return None
    # URL modrinth.com/modpack/<slug>/version/<version>
    # или modrinth.com/mod/<slug>
    if "modrinth.com/" in text:
        # /modpack/<slug> или /mod/<slug>
        import re
        m = re.search(r"modrinth\.com/(?:modpack|mod|resourcepack|shader)/([^/]+)", text)
        if m:
            return m.group(1)
        return None
    # Bare slug/ID — Modrinth slug'и обычно содержат дефисы или lowercase,
    # ID — 8 символов base62. Принимаем любой.
    if "/" in text:
        return None
    return text
