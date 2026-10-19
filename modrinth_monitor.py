"""v2.4.1: Modrinth-мониторинг — polling новых версий модпаков.

Раз в 30 минут опрашивает Modrinth API для каждого подписанного модпака.
Если обнаружена новая версия (id версии отличается от last_version_id в БД):
  - Постит embed в Discord-канал channel_modpacks_id (если настроен)
  - Обновляет last_version_id в БД

Архитектура повторяет github_monitor.py (та же модель polling loop).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

import db
import modrinth
from modrinth import (
    ModrinthError,
    ProjectNotFoundError,
    format_download_url,
    format_filesize,
    format_version_type_emoji,
    get_primary_file,
)

log = logging.getLogger("modrinth_monitor")

POLL_INTERVAL_SECONDS = 1800  # 30 минут
FIRST_CHECK_DELAY = 30        # первая проверка через 30с после старта


async def check_sub_for_updates(guild_id: int, sub: dict) -> dict | None:
    """Проверить одну подписку на новую версию.

    Возвращает dict с инфой о новой версии если есть, иначе None.
    """
    project_id = sub["project_id"]
    last_version_id = sub.get("last_version_id")
    try:
        project = await modrinth.get_project(project_id)
        latest = await modrinth.get_latest_version(project_id)
    except ProjectNotFoundError:
        log.warning("Modrinth project %s not found (404) — skipping", project_id)
        return None
    except ModrinthError as e:
        log.warning("Modrinth API error for %s: %s", project_id, e)
        return None
    except httpx.RequestError as e:
        log.warning("Network error for %s: %s", project_id, e)
        return None

    if not latest:
        return None

    if latest["id"] == last_version_id:
        # Нет новой версии
        return None

    # Новая версия (или первая проверка — last_version_id был None)
    return {
        "project": project,
        "version": latest,
        "is_first_check": last_version_id is None,
    }


async def _post_to_discord(guild_id: int, update: dict) -> None:
    """Постить embed о новом обновлении модпака в Discord."""
    channel_id_str = await db.get_setting("channel_modpacks_id")
    if not channel_id_str or not channel_id_str.isdigit():
        log.info("channel_modpacks_id not configured — skipping Discord post for guild %s",
                 guild_id)
        return

    try:
        import bot as bot_module
        import discord
        bot = bot_module.get_bot_instance()
        if not bot:
            log.debug("Bot not available, skipping Discord post")
            return
        channel = bot.get_channel(int(channel_id_str))
        if channel is None:
            log.warning("Discord channel %s not found", channel_id_str)
            return

        project = update["project"]
        version = update["version"]
        file = get_primary_file(version)

        # Цвет embed по типу версии
        version_type = version.get("version_type", "release")
        color_map = {
            "release": 0x2ECC71,  # зелёный
            "beta":    0xF1C40F,  # жёлтый
            "alpha":   0xE74C3C,  # красный
        }
        color = color_map.get(version_type, 0x95A5A6)

        embed = discord.Embed(
            title=f"📦 {project.get('title', 'Modpack')}",
            description=_build_description(project, version, file),
            color=color,
            url=f"https://modrinth.com/modpack/{project.get('slug', project.get('id'))}",
        )
        if project.get("icon_url"):
            embed.set_thumbnail(url=project["icon_url"])

        embed.add_field(
            name="Версия",
            value=f"{format_version_type_emoji(version_type)} `{version.get('version_number', '?')}`",
            inline=True,
        )
        if version.get("game_versions"):
            embed.add_field(
                name="Game versions",
                value=", ".join(version["game_versions"][:5]),
                inline=True,
            )
        if version.get("loaders"):
            embed.add_field(
                name="Загрузчики",
                value=", ".join(version["loaders"]),
                inline=True,
            )
        if file:
            embed.add_field(
                name="Размер",
                value=format_filesize(file.get("size")),
                inline=True,
            )

        # Changelog (обрезаем если длинный)
        changelog = version.get("changelog", "")
        if changelog:
            # Discord embed field limit — 1024 символа
            max_cl = 1000
            if len(changelog) > max_cl:
                changelog = changelog[:max_cl] + "…"
            embed.add_field(
                name="Изменения",
                value=changelog,
                inline=False,
            )

        if file:
            embed.add_field(
                name="Скачать",
                value=f"[⬇️ {file.get('filename', 'mrpack')}]({format_download_url(file)})",
                inline=False,
            )

        embed.set_footer(
            text=f"Modrinth · опубликовано {version.get('date_published', '?')[:10]}"
        )

        await channel.send(embed=embed)
        log.info("Posted Modrinth update: %s v%s to channel %s",
                 project.get("slug"), version.get("version_number"), channel_id_str)
    except Exception as e:
        log.error("Failed to post Modrinth update to Discord: %s", e, exc_info=True)


def _build_description(project: dict, version: dict, file: dict | None) -> str:
    """Текст под заголовком embed'а."""
    desc = project.get("description", "")
    if desc:
        if len(desc) > 200:
            desc = desc[:200] + "…"
        return desc
    return f"Проект `{project.get('id', '?')}`"


async def check_all_guilds_once() -> None:
    """Один цикл проверки: проходим по всем гильдиям, для каждой — по подпискам.

    При первой проверке (last_version_id == None) НЕ постим — только запоминаем,
    чтобы не заспамить канал при старте бота (когда last_version_id ещё не сохранён).
    """
    try:
        guild_ids = await db.list_approved_guilds()
    except Exception as e:
        log.warning("list_approved_guilds failed: %s", e)
        return

    for guild_id in guild_ids:
        try:
            subs = await db.g_list_modrinth_subs(guild_id)
            if not subs:
                continue
            log.debug("Guild %s: %d modpack subs", guild_id, len(subs))
            for sub in subs:
                update = await check_sub_for_updates(guild_id, sub)
                if not update:
                    continue
                version = update["version"]
                project = update["project"]

                # Обновляем кеш (title, icon_url) — могло поменяться
                await db.g_update_modrinth_sub_cache(
                    guild_id,
                    project_id=sub["project_id"],
                    title=project.get("title", ""),
                    icon_url=project.get("icon_url") or "",
                )

                # Постим только если это НЕ первая проверка
                if update["is_first_check"]:
                    log.info("Initial check for %s: latest version %s (no post)",
                             sub["project_id"], version.get("version_number"))
                else:
                    log.info("New version detected: %s v%s → posting to Discord",
                             sub["project_id"], version.get("version_number"))
                    await _post_to_discord(guild_id, update)

                # Сохраняем новый last_version_id (независимо от постинга)
                await db.g_update_modrinth_sub_last_version(
                    guild_id,
                    project_id=sub["project_id"],
                    version_id=version["id"],
                    version_date=version.get("date_published", ""),
                )
        except Exception as e:
            log.error("Modrinth check failed for guild %s: %s", guild_id, e, exc_info=True)


async def modrinth_monitor_loop() -> None:
    """Фоновый цикл: раз в 30 минут проверяет подписки на новые версии."""
    log.info("Modrinth monitor started (interval=%ds)", POLL_INTERVAL_SECONDS)
    await asyncio.sleep(FIRST_CHECK_DELAY)

    while True:
        try:
            await check_all_guilds_once()
        except asyncio.CancelledError:
            log.info("Modrinth monitor cancelled")
            return
        except Exception as e:
            log.error("Modrinth monitor loop error: %s", e, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def check_once_for_guild(guild_id: int) -> list[dict]:
    """Принудительная проверка для одной гильдии (кнопка 'Проверить сейчас').

    Возвращает список dict'ов {project_id, project, version, is_first_check}
    для найденных новых версий. Не постит в Discord (только возвращает).

    v3.0.4: ВАЖНО — НЕ обновляем last_version_id в БД здесь!
    Раньше функция вызывала g_update_modrinth_sub_last_version() что ломало
    фоновый цикл: когда modrinth_monitor_loop через 30 минут сравнивал
    latest['id'] == last_version_id, они уже были равны (ручная проверка
    их приравняла) → возвращал None → пост в Discord не уходил.

    Теперь: только обновляем кеш (title, icon_url — безвредно), но
    last_version_id остаётся старым до тех пор пока background loop
    не обнаружит новую версию и не запостит в Discord.

    UI на /modpacks пишет «анонс уйдёт в Discord при следующем цикле» —
    это наконец-то правда.
    """
    subs = await db.g_list_modrinth_subs(guild_id)
    results: list[dict] = []
    for sub in subs:
        update = await check_sub_for_updates(guild_id, sub)
        if update:
            results.append({
                "project_id": sub["project_id"],
                "project": update["project"],
                "version": update["version"],
                "is_first_check": update["is_first_check"],
            })
            # Обновляем только кеш (title, icon_url) — безвредно и нужно для UI.
            # last_version_id НЕ ТРОГАЕМ — это работа background loop'а.
            await db.g_update_modrinth_sub_cache(
                guild_id,
                project_id=sub["project_id"],
                title=update["project"].get("title", ""),
                icon_url=update["project"].get("icon_url") or "",
            )
    return results


async def force_post_to_discord(guild_id: int, project_id: str) -> dict:
    """Принудительно запостить в Discord embed о последней версии модпака.

    v3.0.5: вызывается кнопкой «📢 Пост принудительно» на /modpacks.
    Игнорирует last_version_id — постит всегда, даже если версия уже
    анонсировалась ранее. Полезно когда:
    - channel_modpacks_id был только что настроен и хочешь retrospectively
      анонсировать последние версии
    - предыдущий пост упал (Discord был недоступен, бот лежал) — теперь можно
      пере-запостить вручную
    - хочется протестировать формат embed'а

    После постинга обновляет last_version_id в БД — чтобы фоновый цикл
    не запостил дубликат через 30 минут.

    Возвращает dict: {posted: bool, project: dict, version: dict, error: str|None}
    """
    # Найти подписку в БД
    subs = await db.g_list_modrinth_subs(guild_id)
    sub = next((s for s in subs if s["project_id"] == project_id), None)
    if not sub:
        return {"posted": False, "error": f"Подписка с project_id={project_id} не найдена",
                "project": None, "version": None}

    # Получить проект + последнюю версию с Modrinth
    try:
        project = await modrinth.get_project(project_id)
        latest = await modrinth.get_latest_version(project_id)
    except ProjectNotFoundError:
        return {"posted": False, "error": f"Проект {project_id} не найден на Modrinth (404)",
                "project": None, "version": None}
    except ModrinthError as e:
        return {"posted": False, "error": f"Modrinth API error: {e}",
                "project": None, "version": None}
    except httpx.RequestError as e:
        return {"posted": False, "error": f"Network error: {e}",
                "project": None, "version": None}

    if not latest:
        return {"posted": False, "error": "У проекта нет ни одной версии на Modrinth",
                "project": None, "version": None}

    update = {"project": project, "version": latest, "is_first_check": False}

    # Постим в Discord
    try:
        await _post_to_discord(guild_id, update)
    except Exception as e:
        log.error("Force-post to Discord failed for %s: %s", project_id, e, exc_info=True)
        return {"posted": False, "error": f"Discord post failed: {e}",
                "project": project, "version": latest}

    # Обновляем last_version_id чтобы background loop не запостил дубликат
    await db.g_update_modrinth_sub_cache(
        guild_id,
        project_id=project_id,
        title=project.get("title", ""),
        icon_url=project.get("icon_url") or "",
    )
    await db.g_update_modrinth_sub_last_version(
        guild_id,
        project_id=project_id,
        version_id=latest["id"],
        version_date=latest.get("date_published", ""),
    )

    log.info("Force-posted Modrinth update: %s v%s (manual, from /modpacks button)",
             project.get("slug"), latest.get("version_number"))

    return {
        "posted": True,
        "error": None,
        "project": {"title": project.get("title"), "slug": project.get("slug"),
                    "icon_url": project.get("icon_url")},
        "version": {"id": latest.get("id"),
                    "version_number": latest.get("version_number"),
                    "name": latest.get("name"),
                    "date_published": latest.get("date_published"),
                    "version_type": latest.get("version_type")},
    }
