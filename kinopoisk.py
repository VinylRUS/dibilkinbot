"""Kinopoisk API клиент (kinopoisk.dev / poiskkino.dev).
Регистрация: @poiskkinodev_bot в Telegram — выдаёт X-API-KEY.
Бесплатно 200 запросов/день. Русский язык нативно.
Аггрегирует данные Kinopoisk + IMDb + TMDB.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

import db
from config import settings

log = logging.getLogger("kinopoisk")

# Используем legacy домен kinopoisk.dev (всё ещё работает, лучше документирован).
# Альтернатива: api.poiskkino.dev (новый домен, мигрирует на v1.5).
KP_BASE = "https://api.kinopoisk.dev"


class KPDeyError(Exception):
    pass


async def get_token() -> str | None:
    """Получить X-API-KEY из БД (пользователь задаёт через админ-панель) или env."""
    raw = await db.get_setting("kinopoisk_token")
    if raw:
        return crypto.decrypt(raw)
    return getattr(settings, "kinopoisk_token", None) or None


# ВАЖНО: нужен импорт crypto (не сверху, чтобы избежать циклического импорта при инициализации)
from crypto import decrypt as _decrypt


async def get_token() -> str | None:  # type: ignore[no-redef]
    raw = await db.get_setting("kinopoisk_token")
    if raw:
        try:
            return _decrypt(raw)
        except Exception:
            return None
    return getattr(settings, "kinopoisk_token", None) or None


async def _search(client: httpx.AsyncClient, title: str, token: str, limit: int = 5) -> list[dict]:
    """Поиск фильма по названию. Возвращает список dicts."""
    headers = {"X-API-KEY": token, "Accept": "application/json"}
    params = {"query": title, "limit": limit, "page": 1}
    r = await client.get(f"{KP_BASE}/v1.4/movie/search", params=params, headers=headers)
    if r.status_code == 401:
        raise KPDeyError("Kinopoisk: неверный или отсутствует X-API-KEY")
    if r.status_code == 429:
        raise KPDeyError("Kinopoisk: дневной лимит запросов исчерпан (200/день на бесплатном тарифе)")
    if r.status_code >= 400:
        raise KPDeyError(f"Kinopoisk: HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    return data.get("docs", [])


async def _get_details(client: httpx.AsyncClient, kp_id: int, token: str) -> dict:
    """Полные метаданные фильма по Kinopoisk ID."""
    headers = {"X-API-KEY": token, "Accept": "application/json"}
    params: dict = {}
    r = await client.get(f"{KP_BASE}/v1.4/movie/{kp_id}", params=params, headers=headers)
    if r.status_code == 401:
        raise KPDeyError("Kinopoisk: неверный или отсутствует X-API-KEY")
    if r.status_code >= 400:
        raise KPDeyError(f"Kinopoisk: HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def _pick_canonical(movie: dict) -> dict:
    """Нормализованный формат для хранения в БД. Совместим со старым TMDB-контрактом."""
    poster = movie.get("poster") or {}
    rating = movie.get("rating") or {}
    votes = movie.get("votes") or {}
    external = movie.get("externalId") or {}
    release = str(movie.get("year") or "")
    genres = [g["name"] for g in movie.get("genres", []) if g.get("name")]

    # Title: name (RU) → alternativeName (orig) → enName
    title = movie.get("name") or movie.get("alternativeName") or movie.get("enName") or "—"
    original_title = movie.get("alternativeName") or movie.get("enName") or ""

    # plot: shortDescription предпочтительнее (короче, для Discord embed)
    plot = movie.get("shortDescription") or movie.get("description") or ""

    # Используем preview постер (меньше размер) для embed'а
    poster_url = poster.get("previewUrl") or poster.get("url")

    # Рейтинг: приоритет Кинопоиск, но добавим IMDb как опцию
    vote_avg = rating.get("kp") or rating.get("imdb") or rating.get("tmdb") or 0.0
    vote_cnt = votes.get("kp") or votes.get("imdb") or votes.get("tmdb") or 0

    return {
        "tmdb_id": movie.get("id"),  # оставляем поле "tmdb_id" для совместимости со схемой БД
        "title": title,
        "original_title": original_title,
        "year": release or None,
        "release_date": None,  # KP не отдаёт ISO дату, только год
        "poster_url": poster_url,
        "plot": plot,
        "vote_average": round(float(vote_avg), 1) if vote_avg else 0.0,
        "vote_count": int(vote_cnt) if vote_cnt else 0,
        "genres": genres,
        "genres_json": json.dumps(genres, ensure_ascii=False),
        "runtime": movie.get("movieLength"),
        "tagline": None,  # KP не отдаёт тэглайны напрямую
        "imdb_id": external.get("imdb"),
    }


async def lookup_movie(title: str, year: Optional[int] = None) -> Optional[dict]:
    """
    Поиск фильма по названию (русское или оригинальное).
    Возвращает dict с полями. None если не найдено или KP не настроен.
    """
    title = title.strip()
    if not title:
        return None

    # 1. Кеш в БД
    cached = await db.get_movie_meta(title)
    if cached:
        log.debug("KP cache hit for '%s'", title)
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

    # 2. Идём в API
    token = await get_token()
    if not token:
        log.info("KP token not set, skipping lookup for '%s'", title)
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            results = await _search(client, title, token, limit=5)
            if not results:
                log.info("KP: no results for '%s'", title)
                return None
            # Берём первый (API сортирует по релевантности)
            best = results[0]
            # Если есть year в запросе — фильтруем
            if year:
                matching = [r for r in results if r.get("year") == year]
                if matching:
                    best = matching[0]
            # Дополнительно подтягиваем полные данные (для длинного описания, если shortDescription пустое)
            kp_id = best.get("id")
            if kp_id and not best.get("shortDescription"):
                try:
                    full = await _get_details(client, kp_id, token)
                    # merge: дополняем best полными полями
                    for k, v in full.items():
                        if k not in best or not best[k]:
                            best[k] = v
                except KPDeyError as e:
                    log.warning("KP details fetch failed: %s", e)
    except httpx.RequestError as e:
        log.warning("KP request failed: %s", e)
        return None
    except KPDeyError as e:
        log.warning("KP error: %s", e)
        return None

    meta = _pick_canonical(best)

    # 3. Кешируем
    try:
        await db.save_movie_meta(title, meta)
        log.info("KP cached: '%s' → '%s' (kp_id=%s)", title, meta["title"], meta["tmdb_id"])
    except Exception as e:
        log.warning("Failed to cache KP meta: %s", e)

    return meta


async def lookup_by_id(kp_id: int) -> Optional[dict]:
    """Получить метаданные по Kinopoisk ID (для embed'а победителя)."""
    cached = await db.get_movie_meta_by_tmdb_id(kp_id)
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
            "tmdb_id": kp_id,
        }
    token = await get_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            details = await _get_details(client, kp_id, token)
    except (KPDeyError, httpx.RequestError) as e:
        log.warning("KP lookup_by_id failed: %s", e)
        return None
    meta = _pick_canonical(details)
    # Кешируем
    try:
        await db.save_movie_meta(meta["title"], meta)
    except Exception:
        pass
    return meta
