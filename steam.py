"""Steam Web API клиент для модуля Тайный Санта.

Парсит ссылку на профиль Steam → достаёт steam64 ID → получает инфо профиля + wishlist.

Steam API key: https://steamcommunity.com/dev/apikey
Документация: https://partner.steamgames.com/doc/webapi
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

import crypto
import db
from config import settings

log = logging.getLogger("steam")

STEAM_API_BASE = "https://api.steampowered.com"
STEAM_STORE_BASE = "https://store.steampowered.com/api"


async def get_steam_api_key() -> str | None:
    """Получить Steam API key из БД (зашифрован) или из env."""
    raw = await db.get_setting("steam_api_key")
    if raw:
        return crypto.decrypt(raw)
    return getattr(settings, "steam_api_key", None) or None


def parse_steam_profile_url(url: str) -> tuple[str | None, str | None]:
    """Парсит ссылку на профиль Steam. Возвращает (тип, значение).

    Поддерживаемые форматы:
    - https://steamcommunity.com/profiles/76561198012345678 → ('id64', '76561198012345678')
    - https://steamcommunity.com/id/vinylrus → ('vanity', 'vinylrus')
    - 76561198012345678 → ('id64', '76561198012345678')
    - vinylrus → ('vanity', 'vinylrus')

    Возвращает (None, None) если не распознано.
    """
    if not url:
        return None, None
    url = url.strip()

    # Прямой steam64 ID (17-значное число, начинается с 765611)
    if re.match(r"^765611\d{11}$", url):
        return ("id64", url)

    # vanity name (только буквы/цифры/дефисы, не длиннее 32)
    if re.match(r"^[a-zA-Z0-9_-]{3,32}$", url) and "/" not in url:
        return ("vanity", url)

    # URL с /profiles/
    m = re.search(r"steamcommunity\.com/profiles/(\d+)", url)
    if m:
        return ("id64", m.group(1))

    # URL с /id/
    m = re.search(r"steamcommunity\.com/id/([a-zA-Z0-9_-]+)", url)
    if m:
        return ("vanity", m.group(1))

    return None, None


async def resolve_vanity_to_id64(vanity_name: str, api_key: str) -> str | None:
    """Преобразовать vanity name в steam64 ID через Steam API.
    Возвращает steam64 ID или None если не найден.
    """
    url = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1/"
    params = {"key": api_key, "vanityurl": vanity_name}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            log.warning("Steam ResolveVanityURL HTTP %s: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        if data.get("response", {}).get("success") != 1:
            log.info("Steam vanity '%s' not resolved: %s", vanity_name, data.get("response", {}).get("message"))
            return None
        return data["response"].get("steamid")
    except Exception as e:
        log.warning("Steam ResolveVanityURL failed: %s", e)
        return None


async def get_player_summaries(steam_id64: str, api_key: str) -> dict | None:
    """Получить инфо о профиле через Steam API.
    Возвращает dict: {steamid, personaname, avatar, avatarmedium, avatarfull, profileurl, communityvisibilitystate}
    или None если не найден.

    communityvisibilitystate: 1 = приватный, 3 = публичный.
    """
    url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
    params = {"key": api_key, "steamids": steam_id64}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            log.warning("Steam GetPlayerSummaries HTTP %s: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        players = data.get("response", {}).get("players", [])
        if not players:
            return None
        p = players[0]
        return {
            "steamid": p.get("steamid"),
            "personaname": p.get("personaname"),
            "avatar": p.get("avatar"),
            "avatarmedium": p.get("avatarmedium"),
            "avatarfull": p.get("avatarfull"),
            "profileurl": p.get("profileurl"),
            "communityvisibilitystate": p.get("communityvisibilitystate", 1),
            "is_public": p.get("communityvisibilitystate") == 3,
        }
    except Exception as e:
        log.warning("Steam GetPlayerSummaries failed: %s", e)
        return None


# v2.1.0: Новые функции для трекинга игр пользователя

async def get_recently_played_games(steam_id64: str, api_key: str, count: int = 10) -> list[dict] | None:
    """Получить последние сыгранные игры пользователя (за последние 2 недели).

    Endpoint: IPlayerService/GetRecentlyPlayedGames/v1/
    Требует публичный профиль (communityvisibilitystate=3).

    Возвращает список dict'ов (имена полей с _min суффиксом, чтобы совпадать с кешем в БД):
    - appid: int — Steam app ID
    - name: str — название игры
    - playtime_2weeks_min: int — минуты за последние 2 недели
    - playtime_forever_min: int — общее время в минутах
    - img_icon_url: str — hash иконки (для построения URL Steam CDN)
    - img_logo_url: str — hash логотипа
    - icon_url: str — готовый URL иконки (построен из img_icon_url)
    - logo_url: str — готовый URL логотипа

    Возвращает None если профиль приватный или нет данных.
    Возвращает [] если профиль публичный, но юзер ничего не играл за 2 недели.
    """
    url = f"{STEAM_API_BASE}/IPlayerService/GetRecentlyPlayedGames/v1/"
    params = {"key": api_key, "steamid": steam_id64, "count": count}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            log.warning("Steam GetRecentlyPlayedGames HTTP %s: %s", r.status_code, r.text[:200])
            return None
        data = r.json().get("response", {})
        if data.get("total_count", 0) == 0:
            return []
        games = data.get("games", [])
        result = []
        for g in games:
            icon_hash = g.get("img_icon_url", "")
            logo_hash = g.get("img_logo_url", "")
            result.append({
                "appid": g.get("appid"),
                "name": g.get("name", "Unknown"),
                "playtime_2weeks_min": g.get("playtime_2weeks", 0),
                "playtime_forever_min": g.get("playtime_forever", 0),
                "img_icon_url": icon_hash,
                "img_logo_url": logo_hash,
                "icon_url": f"https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{g['appid']}/{icon_hash}.ico" if icon_hash else None,
                "logo_url": f"https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{g['appid']}/{logo_hash}.jpg" if logo_hash else None,
            })
        return result
    except Exception as e:
        log.warning("Steam GetRecentlyPlayedGames failed: %s", e)
        return None


async def get_owned_games(steam_id64: str, api_key: str, include_appinfo: bool = True, include_free_games: bool = True) -> list[dict] | None:
    """Получить полную библиотеку игр пользователя (со временем игры).

    Endpoint: IPlayerService/GetOwnedGames/v1/
    Требует публичный профиль.

    Возвращает список dict'ов (имена полей с _min суффиксом, чтобы совпадать с кешем в БД):
    - appid: int
    - name: str (только если include_appinfo=True)
    - playtime_forever_min: int — минуты за всё время
    - playtime_2weeks_min: int — минуты за последние 2 недели (есть не всегда)
    - img_icon_url: str — hash для построения URL иконки
    - icon_url: str — готовый URL

    Возвращает None если профиль приватный или нет данных.
    Возвращает [] если профиль публичный, но юзер не имеет игр.
    """
    url = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": api_key,
        "steamid": steam_id64,
        "include_appinfo": "true" if include_appinfo else "false",
        "include_played_free_games": "true" if include_free_games else "false",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            log.warning("Steam GetOwnedGames HTTP %s: %s", r.status_code, r.text[:200])
            return None
        data = r.json().get("response", {})
        if data.get("game_count", 0) == 0:
            return []
        games = data.get("games", [])
        result = []
        for g in games:
            icon_hash = g.get("img_icon_url", "")
            appid = g.get("appid")
            result.append({
                "appid": appid,
                "name": g.get("name", "Unknown"),
                "playtime_forever_min": g.get("playtime_forever", 0),
                "playtime_2weeks_min": g.get("playtime_2weeks", 0),
                "img_icon_url": icon_hash,
                "icon_url": f"https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.ico" if icon_hash and appid else None,
            })
        # Сортируем по убыванию playtime_forever — топ игр первыми
        result.sort(key=lambda x: x.get("playtime_forever_min", 0), reverse=True)
        return result
    except Exception as e:
        log.warning("Steam GetOwnedGames failed: %s", e)
        return None


async def get_wishlist(steam_id64: str) -> list[dict] | None:
    """Получить wishlist игр Steam-профиля.

    Steam не имеет официального API для wishlist, но есть неофициальный endpoint:
    https://store.steampowered.com/api/wishlist/wishlist/{steam_id64}/

    Возвращает list of dicts: {appid, name, capsule_filename, ...} или None если приватный/ошибка.
    Возвращает [] если вишлист пустой.
    """
    url = f"{STEAM_STORE_BASE}/wishlist/wishlist/{steam_id64}/"
    try:
        # Steam отдаёт JSON, но иногда может потребовать cookies/headers
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; DeeBeelkinBot/1.0)",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
            r = await client.get(url)
        if r.status_code == 401 or r.status_code == 403:
            log.info("Steam wishlist for %s is private (HTTP %s)", steam_id64, r.status_code)
            return None
        if r.status_code != 200:
            log.warning("Steam wishlist HTTP %s: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        if not isinstance(data, dict):
            return []
        # data — dict где ключи — appid (строки), значения — dict с инфо об игре
        games = []
        for appid_str, info in data.items():
            if not isinstance(info, dict):
                continue
            games.append({
                "appid": int(appid_str),
                "name": info.get("name", f"App {appid_str}"),
                "capsule": info.get("capsule"),
                "review_desc": info.get("review_desc", ""),
                "review_total": info.get("reviews_total", 0),
                "review_percent": info.get("reviews_percent", 0),
                "type": info.get("type", ""),
                "release_date": info.get("release_string", ""),
                "subtitle": info.get("subtitle", ""),
            })
        # Сортируем по рейтингу (самые популярные первыми)
        games.sort(key=lambda g: g.get("review_total", 0), reverse=True)
        return games
    except Exception as e:
        log.warning("Steam get_wishlist failed for %s: %s", steam_id64, e)
        return None


async def validate_steam_profile(profile_url_or_id: str) -> dict:
    """Полная валидация Steam-профиля.
    Возвращает dict с полями:
    - valid: bool
    - error: str | None (сообщение об ошибке если invalid)
    - steam_id64: str | None
    - steam_persona: str | None
    - steam_avatar_url: str | None
    - steam_profile_url: str | None
    - is_public: bool (профиль публичный?)
    - wishlist_public: bool | None (None если не удалось проверить)
    """
    result = {
        "valid": False, "error": None,
        "steam_id64": None, "steam_persona": None,
        "steam_avatar_url": None, "steam_profile_url": None,
        "is_public": False, "wishlist_public": None,
    }

    # Парсим URL
    profile_type, value = parse_steam_profile_url(profile_url_or_id)
    if not profile_type:
        result["error"] = "Не удалось распознать ссылку. Поддерживаются форматы: steamcommunity.com/profiles/7656... или steamcommunity.com/id/username или просто steam64 ID."
        return result

    # Получаем API key
    api_key = await get_steam_api_key()
    if not api_key:
        result["error"] = "Steam API key не задан. Попросите админа добавить его в /tokens."
        return result

    # Если vanity — резолвим в steam64
    if profile_type == "vanity":
        steam_id64 = await resolve_vanity_to_id64(value, api_key)
        if not steam_id64:
            result["error"] = f"Имя '{value}' не найдено в Steam. Проверьте правильность."
            return result
    else:
        steam_id64 = value

    result["steam_id64"] = steam_id64

    # Получаем инфо профиля
    summary = await get_player_summaries(steam_id64, api_key)
    if not summary:
        result["error"] = "Профиль не найден. Возможно, steam64 ID неверный."
        return result

    result["steam_persona"] = summary.get("personaname")
    result["steam_avatar_url"] = summary.get("avatarfull") or summary.get("avatarmedium") or summary.get("avatar")
    result["steam_profile_url"] = summary.get("profileurl") or f"https://steamcommunity.com/profiles/{steam_id64}"
    result["is_public"] = summary.get("is_public", False)

    if not result["is_public"]:
        result["error"] = "Профиль приватный. Сделайте профиль публичным (Steam → Настройки → Приватность), чтобы санта мог увидеть ваш вишлист."
        return result

    # Проверяем wishlist
    wishlist = await get_wishlist(steam_id64)
    if wishlist is None:
        result["wishlist_public"] = False
        # Не блокируем — профиль публичный, но вишлист может быть скрыт отдельно
        result["valid"] = True
        result["error"] = None  # профиль валиден, но вишлист недоступен
        return result

    result["wishlist_public"] = True
    result["valid"] = True
    return result


async def get_top_wishlist_games(steam_id64: str, limit: int = 10) -> list[dict]:
    """Получить топ N игр из вишлиста (по количеству отзывов = популярность).
    Возвращает список: [{appid, name, ...}] или пустой список.
    """
    wishlist = await get_wishlist(steam_id64)
    if not wishlist:
        return []
    return wishlist[:limit]
