"""Kinopoisk API клиент через kinopoiskapiunofficial.tech.

Регистрация: https://kinopoiskapiunofficial.tech/signup (email + подтверждение).
API-ключ: https://kinopoiskapiunofficial.tech/profile после входа.

Особенности:
- Header X-API-KEY для авторизации (как у kinopoisk.dev — совместимость)
- Возвращает чистый JSON для ВСЕХ ответов (без 301-редиректов на HTML)
- Бесплатный тариф: ~500 запросов/день, 25 req/sec
- Русские названия нативно: nameRu, nameEn, nameOriginal на каждом фильме

Эндпоинты:
- /api/v2.1/films/search-by-keyword?keyword=X → поиск по названию (5 req/sec)
- /api/v2.2/films/{id} → полные метаданные по ID (25 req/sec)

Коды ответов:
- 200 OK
- 401 — неверный или пустой X-API-KEY
- 402 — дневной/общий лимит исчерпан (нестандартно, не payment required)
- 404 — фильм не найден
- 429 — per-second rate limit
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

import db
from config import settings

log = logging.getLogger("kinopoisk")

KP_BASE = "https://kinopoiskapiunofficial.tech"


class KPDeyError(Exception):
    pass


async def get_token() -> str | None:
    """Получить X-API-KEY из БД или env."""
    raw = await db.get_setting("kinopoisk_token")
    if raw:
        try:
            from crypto import decrypt
            return decrypt(raw)
        except Exception:
            return None
    return getattr(settings, "kinopoisk_token", None) or None


def _headers(token: str) -> dict[str, str]:
    return {"X-API-KEY": token, "Content-Type": "application/json"}


async def _safe_json(r: httpx.Response, context: str) -> dict:
    """Распарсить JSON ответ с защитой от HTML/пустых тел.
    Всегда возвращает dict (даже если в ответе список — берём первый элемент).
    """
    # Проверяем Content-Type — kinopoiskapiunofficial.tech всегда отдаёт application/json
    content_type = r.headers.get("content-type", "")
    if "json" not in content_type.lower():
        log.warning("%s: unexpected Content-Type '%s', body[:200]=%r",
                    context, content_type, r.text[:200])
        raise KPDeyError(f"Kinopoisk: ожидался JSON, получен {content_type}")

    try:
        data = r.json()
    except Exception as e:
        log.warning("%s: JSON parse failed (body[:200]=%r)", context, r.text[:200])
        raise KPDeyError(f"Kinopoisk: невалидный JSON ответ") from e

    if not isinstance(data, (dict, list)):
        raise KPDeyError(f"Kinopoisk: неожиданный тип ответа: {type(data).__name__}")

    return data if isinstance(data, dict) else {"items": data}


async def _search(client: httpx.AsyncClient, title: str, token: str) -> list[dict]:
    """Поиск фильма по названию. Возвращает список сырых фильмов (v2.1 format)."""
    r = await client.get(
        f"{KP_BASE}/api/v2.1/films/search-by-keyword",
        params={"keyword": title, "page": 1},
        headers=_headers(token),
    )

    if r.status_code == 401:
        raise KPDeyError("Kinopoisk: неверный или отсутствует X-API-KEY")
    if r.status_code == 402:
        raise KPDeyError("Kinopoisk: дневной/общий лимит запросов исчерпан (FREE ≈ 500/день)")
    if r.status_code == 429:
        raise KPDeyError("Kinopoisk: per-second rate limit (5 req/sec для search)")
    if r.status_code == 404:
        return []  # не найдено — это не ошибка
    if r.status_code >= 400:
        # Пытаемся достать message из JSON
        try:
            err_data = await _safe_json(r, "search error")
            msg = err_data.get("message", r.text[:200])
        except KPDeyError:
            msg = r.text[:200]
        raise KPDeyError(f"Kinopoisk: HTTP {r.status_code}: {msg}")

    data = await _safe_json(r, "search")
    return data.get("films", [])


async def _get_details(client: httpx.AsyncClient, film_id: int, token: str) -> dict:
    """Полные метаданные фильма по Kinopoisk ID (v2.2)."""
    r = await client.get(
        f"{KP_BASE}/api/v2.2/films/{film_id}",
        headers=_headers(token),
    )

    if r.status_code == 401:
        raise KPDeyError("Kinopoisk: неверный или отсутствует X-API-KEY")
    if r.status_code == 402:
        raise KPDeyError("Kinopoisk: дневной/общий лимит запросов исчерпан")
    if r.status_code == 404:
        raise KPDeyError(f"Kinopoisk: фильм с id={film_id} не найден")
    if r.status_code == 429:
        raise KPDeyError("Kinopoisk: per-second rate limit")
    if r.status_code >= 400:
        try:
            err_data = await _safe_json(r, "details error")
            msg = err_data.get("message", r.text[:200])
        except KPDeyError:
            msg = r.text[:200]
        raise KPDeyError(f"Kinopoisk: HTTP {r.status_code}: {msg}")

    return await _safe_json(r, "details")


def _pick_canonical(movie: dict) -> dict:
    """Нормализованный формат для хранения в БД.
    Совместим со старым TMDB-контрактом (поля tmdb_id и т.д. — не переименовываем).
    """
    # Названия: приоритет nameRu → nameOriginal → nameEn
    title = movie.get("nameRu") or movie.get("nameOriginal") or movie.get("nameEn") or "—"
    original_title = movie.get("nameOriginal") or movie.get("nameEn") or ""

    # Год: в v2.2 это int, в v2.1 search-by-keyword это string
    year_raw = movie.get("year")
    if year_raw is None:
        year_str = None
    elif isinstance(year_raw, int):
        year_str = str(year_raw)
    else:
        year_str = str(year_raw)

    # Постер: preview меньше, берём его для embed'ов
    poster_url = movie.get("posterUrlPreview") or movie.get("posterUrl")

    # Описание: короткое предпочтительнее (для Discord embed)
    plot = movie.get("shortDescription") or movie.get("description") or ""

    # Жанры: список dicts [{"genre": "фантастика"}, ...] → ["фантастика", ...]
    genres = [g["genre"] for g in movie.get("genres", []) if isinstance(g, dict) and g.get("genre")]

    # Рейтинг: приоритет Кинопоиск, fallback на IMDb
    vote_avg = movie.get("ratingKinopoisk") or movie.get("ratingImdb") or movie.get("rating") or 0.0
    vote_cnt = (movie.get("ratingKinopoiskVoteCount") or movie.get("ratingImdbVoteCount")
                or movie.get("ratingVoteCount") or 0)

    # В v2.1 search-by-keyword поле "rating" — это строка "8.5"
    if isinstance(vote_avg, str):
        try:
            vote_avg = float(vote_avg)
        except ValueError:
            vote_avg = 0.0

    # filmLength: в v2.2 это int (минуты), в v2.1 это строка "02:16"
    runtime = movie.get("filmLength")
    if isinstance(runtime, str):
        # "02:16" → 136 минут
        parts = runtime.split(":")
        if len(parts) == 2:
            try:
                runtime = int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                runtime = None
        else:
            runtime = None

    # ID: в v2.2 это "kinopoiskId", в v2.1 это "filmId"
    kp_id = movie.get("kinopoiskId") or movie.get("filmId")

    return {
        "tmdb_id": kp_id,  # оставляем имя tmdb_id для совместимости со схемой БД
        "title": title,
        "original_title": original_title,
        "year": year_str,
        "release_date": None,
        "poster_url": poster_url,
        "plot": plot,
        "vote_average": round(float(vote_avg), 1) if vote_avg else 0.0,
        "vote_count": int(vote_cnt) if vote_cnt else 0,
        "genres": genres,
        "genres_json": json.dumps(genres, ensure_ascii=False),
        "runtime": runtime,
        "tagline": movie.get("slogan"),
        "imdb_id": movie.get("imdbId"),
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
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            results = await _search(client, title, token)
            if not results:
                log.info("KP: no results for '%s'", title)
                return None

            # Берём первый (API сортирует по релевантности)
            best = results[0]

            # Если есть year в запросе — фильтруем
            if year:
                matching = [r for r in results if str(r.get("year")) == str(year)]
                if matching:
                    best = matching[0]

            # Подтягиваем полные детали через v2.2 (для plot, imdb_id, runtime в int)
            film_id = best.get("filmId") or best.get("kinopoiskId")
            if film_id:
                try:
                    full = await _get_details(client, film_id, token)
                    # merge: дополняем best полными полями
                    for k, v in full.items():
                        if k not in best or not best[k]:
                            best[k] = v
                except KPDeyError as e:
                    log.warning("KP details fetch failed (using search result only): %s", e)
    except httpx.RequestError as e:
        log.warning("KP request failed: %s", e)
        return None
    except KPDeyError as e:
        log.warning("KP error: %s", e)
        return None

    meta = _pick_canonical(best)

    # 3. Кешируем в БД
    try:
        await db.save_movie_meta(title, meta)
        log.info("KP cached: '%s' → '%s' (kp_id=%s)", title, meta["title"], meta["tmdb_id"])
    except Exception as e:
        log.warning("Failed to cache KP meta: %s", e)

    return meta


async def lookup_by_id(kp_id: int) -> Optional[dict]:
    """Получить метаданные по Kinopoisk ID. Используется при показе победителей."""
    # Сначала кеш
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
    # Идём в API
    token = await get_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
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
