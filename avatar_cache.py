"""Avatar caching — downloads Discord CDN avatars, serves them locally.

v3.4: solves two problems:
1. Discord CDN URLs become stale (user changes avatar → old hash 404s)
2. Browser can't reach Discord CDN (BotHost proxy restrictions)

Flow:
- GET /avatars/{discord_id} endpoint checks local cache
- If cached → serve file (fast, no external request)
- If not cached → try download from stored Discord URL → cache → serve
- If download fails → generate SVG letter avatar → serve
- On bot startup → refresh all avatars from current guild members
- On on_user_update (avatar change) → re-download

Storage: /app/data/avatars/{discord_id}.png
Size: ~2-5 KB per avatar (128x128), 100 users = ~500 KB total
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

import httpx

import db

log = logging.getLogger("avatar_cache")

# Directory for cached avatars — same parent as bot.db
_DATA_DIR = Path(os.environ.get("DATABASE_PATH", "/app/data/bot.db")).parent
AVATAR_DIR = _DATA_DIR / "avatars"

# Ensure directory exists
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

# Avatar size to download from Discord (128 is enough for UI, small file)
DISCORD_AVATAR_SIZE = 128

# Colors for letter avatars (cycled by discord_id hash)
AVATAR_COLORS = [
    "#FFB703",  # accent gold
    "#FB8500",  # orange
    "#43B581",  # green
    "#5865F2",  # blurple
    "#E4583E",  # red
    "#9D4EDD",  # purple
    "#06A77D",  # teal
    "#F77F00",  # amber
]


def get_avatar_path(discord_id: int) -> Path:
    """Local file path for cached avatar."""
    return AVATAR_DIR / f"{int(discord_id)}.png"


def get_avatar_url(discord_id: int | str) -> str:
    """URL for serving cached avatar via our FastAPI endpoint.
    Use this in templates instead of raw Discord CDN URL."""
    return f"/avatars/{int(discord_id)}"


def _color_for_id(discord_id: int) -> str:
    """Pick a color based on discord_id (deterministic)."""
    return AVATAR_COLORS[int(discord_id) % len(AVATAR_COLORS)]


def generate_letter_avatar_svg(name: str, discord_id: int) -> str:
    """Generate SVG with first letter of name in a colored circle.
    No image library needed — just string formatting."""
    letter = (str(name).strip()[0] if name and str(name).strip() else "?").upper()
    color = _color_for_id(discord_id)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">'
        f'<circle cx="64" cy="64" r="64" fill="{color}22"/>'
        f'<circle cx="64" cy="64" r="63" fill="none" stroke="{color}" stroke-width="2"/>'
        f'<text x="64" y="82" font-size="56" font-weight="bold" '
        f'text-anchor="middle" fill="{color}" font-family="sans-serif">{letter}</text>'
        f'</svg>'
    )


async def download_avatar(discord_id: int, discord_url: str | None) -> bool:
    """Download avatar from Discord CDN, save locally.
    Returns True on success, False on failure (404, network error, etc.)."""
    if not discord_url:
        return False

    # Ensure URL has size param (smaller download)
    url = discord_url
    if "size=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}size={DISCORD_AVATAR_SIZE}"
    elif "size=1024" in url:
        url = url.replace("size=1024", f"size={DISCORD_AVATAR_SIZE}")

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code != 200 or len(r.content) < 100:
            log.debug("Avatar download failed for %s: HTTP %s, %d bytes",
                      discord_id, r.status_code, len(r.content))
            return False

        # Save to cache
        path = get_avatar_path(discord_id)
        path.write_bytes(r.content)
        log.debug("Avatar cached: %s (%d bytes)", discord_id, len(r.content))
        return True
    except Exception as e:
        log.debug("Avatar download error for %s: %s", discord_id, e)
        return False


async def ensure_cached(discord_id: int) -> bool:
    """Ensure avatar is cached. If not cached, try to download from DB stored URL.
    Returns True if cached (or was just downloaded), False if unavailable."""
    path = get_avatar_path(discord_id)
    if path.exists() and path.stat().st_size > 100:
        return True

    # Not cached — get URL from DB and try download
    user = await db.get_user(discord_id)
    if not user:
        return False
    avatar_url = user[4]  # avatar_url is 5th column (index 4)
    if not avatar_url:
        return False

    return await download_avatar(discord_id, avatar_url)


async def refresh_avatar(discord_id: int, discord_url: str | None) -> bool:
    """Force re-download avatar (e.g., when user changes avatar on Discord).
    Deletes old cache first, then downloads new."""
    path = get_avatar_path(discord_id)
    if path.exists():
        path.unlink()
    return await download_avatar(discord_id, discord_url)


async def refresh_all_avatars() -> int:
    """Download/refresh avatars for all current guild members.
    Called on bot startup. Returns count of successfully cached avatars."""
    try:
        import bot as bot_module
        bot = bot_module.get_bot_instance()
        if not bot:
            log.warning("Bot not available, skipping avatar refresh")
            return 0
    except Exception:
        return 0

    count = 0
    failed = 0
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            try:
                avatar_url = str(member.display_avatar.url) if member.display_avatar else None
                if avatar_url:
                    # v3.4.1: Don't call upsert_user here — it was causing
                    # username/display_name to be None'd (before the COALESCE fix).
                    # Avatar URL is already updated by _auto_create_member_profiles
                    # which runs on the same startup. We just download the file.
                    ok = await download_avatar(member.id, avatar_url)
                    if ok:
                        count += 1
                    else:
                        failed += 1
            except Exception as e:
                log.debug("Avatar refresh failed for %s: %s", member.id, e)
                failed += 1

    log.info("Avatar cache: %d downloaded, %d failed", count, failed)
    return count
