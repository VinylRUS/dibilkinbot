"""TMDB клиент с кешем в БД на 7 дней. Бесплатно, поддерживает русский язык."""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

import db
from config import settings

log = logging.getLogger("tmdb")

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"
LANG = "ru-RU"
POSTER_SIZE = "w500"


class TMDBError(Exception):
    pass


async def get_token() -> str | None:
    """Получить Bearer token из БД (пользователь задаёт через админ-панель) или env."""
    from crypto import decrypt
    from config import settings as s
    raw = await db.get_setting("tmdb_token")
    if raw:
        return decrypt(raw)
    # Fallback на env (если задан)
    return getattr(s, "tmdb_token", None) or None


async def _search_movie(client: httpx.AsyncClient, title: str, token: str, year: Optional[int] = None) -> list[dict]:
    params = {"query": title, "language": LANG, "include_adult": "false"}
    if year:
        params["year"] = str(year)
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    r = await client.get(f"{TMDB_BASE}/search/movie", params=params, headers=headers)
    if r.status_code == 401:
        raise TMDBError("TMDB: неверный Bearer token")
    if r.status_code >= 400:
        raise TMDBError(f"TMDB: HTTP {r.status_code}: {r.text[:200]}")
    return r.json().get("results", [])


async def _get_details(client: httpx.AsyncClient, tmdb_id: int, token: str) -> dict:
    params = {"language": LANG}
    headers = {"Authorization": f"Bearer {token}", "accept": "application/json"}
    r = await client.get(f"{TMDB_BASE}/movie/{tmdb_id}", params=params, headers=headers)
    if r.status_code >= 400:
        raise TMDBError(f"TMDB: HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _poster(path: Optional[str]) -> Optional[str]:
    return f"{IMG_BASE}/{POSTER_SIZE}{path}" if path else None


async def lookup_movie(title: str, year: Optional[int] = None) -> Optional[dict]:
    """
    Поиск фильма по названию (русское или оригинальное).
    Возвращает dict с полями: tmdb_id, title, original_title, year, release_date,
    poster_url, plot, vote_average, vote_count, genres, runtime, tagline, imdb_id.
    None если не найдено или TMDB не настроен.
    """
    title = title.strip()
    if not title:
        return None

    # 1. Проверяем кеш в БД
    cached = await db.get_movie_meta(title)
    if cached:
        log.debug("TMDB cache hit for '%s'", title)
        return {
            "tmdb_id": cached[0],
            "title": cached[1],
            "original_title": cached[2],
            "year": cached[3],
            "release_date": cached[4],
            "poster_url": cached[5],
            "plot": cached[6],
            "vote_average": cached[7],
            "vote_count": cached[8],
            "genres": json.loads(cached[9]) if cached[9] else [],
            "runtime": cached[10],
            "tagline": cached[11],
            "imdb_id": cached[12],
        }

    # 2. Идём в TMDB API
    token = await get_token()
    if not token:
        log.info("TMDB token not set, skipping lookup for '%s'", title)
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            results = await _search_movie(client, title, token, year)
            if not results:
                log.info("TMDB: no results for '%s'", title)
                return None
            # Берём самый популярный результат
            best = max(results, key=lambda m: m.get("popularity", 0))
            details = await _get_details(client, best["id"], token)
    except httpx.RequestError as e:
        log.warning("TMDB request failed: %s", e)
        return None
    except TMDBError as e:
        log.warning("TMDB error: %s", e)
        return None

    release = details.get("release_date") or ""
    meta = {
        "tmdb_id": details["id"],
        "title": details.get("title") or details.get("original_title", ""),
        "original_title": details.get("original_title", ""),
        "year": release[:4] if release else None,
        "release_date": release or None,
        "poster_url": _poster(details.get("poster_path")),
        "plot": details.get("overview") or "",
        "vote_average": round(details.get("vote_average") or 0.0, 1),
        "vote_count": details.get("vote_count") or 0,
        "genres": [g["name"] for g in details.get("genres", [])],
        "genres_json": json.dumps([g["name"] for g in details.get("genres", [])], ensure_ascii=False),
        "runtime": details.get("runtime"),
        "tagline": details.get("tagline") or None,
        "imdb_id": details.get("imdb_id"),
    }

    # 3. Кешируем в БД
    try:
        await db.save_movie_meta(title, meta)
        log.info("TMDB cached: '%s' → '%s' (tmdb_id=%s)", title, meta["title"], meta["tmdb_id"])
    except Exception as e:
        log.warning("Failed to cache TMDB meta: %s", e)

    return meta


async def lookup_by_id(tmdb_id: int) -> Optional[dict]:
    """Получить метаданные по TMDB ID (используется при показе постеров для победителей)."""
    cached = await db.get_movie_meta_by_tmdb_id(tmdb_id)
    if cached:
        return {
            "title": cached[0],
            "year": cached[1],
            "poster_url": cached[2],
            "plot": cached[3],
            "vote_average": cached[4],
            "vote_count": cached[5],
            "genres": json.loads(cached[6]) if cached[6] else [],
            "runtime": cached[7],
            "tagline": cached[8],
            "imdb_id": cached[9],
            "tmdb_id": tmdb_id,
        }
    # Нет в кеше — идём в API
    token = await get_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            details = await _get_details(client, tmdb_id, token)
    except (TMDBError, httpx.RequestError) as e:
        log.warning("TMDB lookup_by_id failed: %s", e)
        return None
    release = details.get("release_date") or ""
    return {
        "tmdb_id": details["id"],
        "title": details.get("title") or "",
        "year": release[:4] if release else None,
        "poster_url": _poster(details.get("poster_path")),
        "plot": details.get("overview") or "",
        "vote_average": round(details.get("vote_average") or 0.0, 1),
        "vote_count": details.get("vote_count") or 0,
        "genres": [g["name"] for g in details.get("genres", [])],
        "runtime": details.get("runtime"),
        "tagline": details.get("tagline"),
        "imdb_id": details.get("imdb_id"),
    }
