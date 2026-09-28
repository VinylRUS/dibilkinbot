"""Discord-бот DeeBeelkin.

Cog'и:
- QuotesCog: /funword + авто-захват цитат в канале #цитатник и реакцией
- MovieNightCog: /movienight — анонс киновечера + Discord Scheduled Event
- WatchlistCog: /addfilm, /delfilm, /myfilms — личный список желаемого (ephemeral)
- SettingsCog: /setquoteemoji, /changelog
- LinkCog: /linktg — привязка Telegram для личных уведомлений

Также: on_wheel_spin_completed — постинг победителя в Discord+TG, вызывается из web.py.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import crypto
import db
import telegram
from config import settings
from telegram import send_message, tg_escape

log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = True  # нужен для авто-захвата цитат (on_message + реакции)
intents.guilds = True
intents.members = True
intents.reactions = True  # нужен для on_raw_reaction_add (захват цитат реакцией)


# === Проверка member сервера ===

async def is_guild_member(discord_id: int) -> tuple[bool, dict | None]:
    """Проверить, является ли discord_id участником какого-либо сервера с ботом.

    Возвращает (True, member_info_dict) если да, иначе (False, None).
    member_info содержит: display_name, username, avatar_url, top_role_name, roles.
    """
    if not _bot_running:
        return False, None
    bot = _get_bot()
    if bot is None:
        return False, None

    for guild in bot.guilds:
        member = guild.get_member(discord_id)
        if member is None:
            # нет в кеше — пробуем fetch
            try:
                member = await guild.fetch_member(discord_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue
        if member is not None:
            # Собираем информацию о пользователе
            roles = [r.name for r in member.roles if r.name != "@everyone"]
            top_role = member.top_role.name if member.top_role and member.top_role.name != "@everyone" else None
            return True, {
                "display_name": member.display_name or member.name,
                "username": str(member),
                "avatar_url": str(member.display_avatar.url) if member.display_avatar else None,
                "guild_name": guild.name,
                "guild_id": guild.id,
                "roles": roles,
                "top_role": top_role,
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "is_owner": guild.owner_id == discord_id,
            }
    return False, None


# Глобальные ссылки для доступа из web.py
_bot_running = False
_bot_ref = None


def _get_bot():
    return _bot_ref


def get_bot_instance():
    """Публичный accessor для web.py / main.py — возвращает активный bot или None."""
    return _bot_ref if _bot_running else None


def _set_bot(b):
    global _bot_ref
    _bot_ref = b


def _set_bot_running(running: bool):
    global _bot_running
    _bot_running = running


# === Токены: приоритет из БД (зашифрованы), fallback на env ===

async def get_token(key: str, env_value: str | None) -> str | None:
    raw = await db.get_setting(key)
    if raw:
        return crypto.decrypt(raw)
    return env_value


async def get_tg_config() -> tuple[str | None, str | None]:
    tg_token = await get_token("telegram_token", settings.telegram_token)
    tg_chat = await db.get_setting("telegram_chat_id") or settings.telegram_chat_id
    return tg_token, tg_chat


async def tg_crosspost(text: str, chat_id: str | None = None) -> None:
    """Отправить сообщение в TG. Молча пропускает если не настроено."""
    tg_token, default_chat = await get_tg_config()
    if not tg_token:
        return
    target_chat = chat_id or default_chat
    if not target_chat:
        return
    await send_message(tg_token, target_chat, text)


# === Бот ===

class KinovecherBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self._user_cache = {}  # discord_id → last_seen_username (для оптимизации)

    async def setup_hook(self) -> None:
        cogs = [
            ("QuotesCog", QuotesCog),
            ("MovieNightCog", MovieNightCog),
            ("WatchlistCog", WatchlistCog),
            ("SettingsCog", SettingsCog),
            ("LinkCog", LinkCog),
        ]
        for name, cls in cogs:
            try:
                await self.add_cog(cls(self))
                log.info("✓ Cog loaded: %s", name)
            except Exception as e:
                log.error("✗ Failed to load cog %s: %r", name, e, exc_info=True)

        all_cmds = self.tree.get_commands()
        log.info("Tree after setup_hook: %d commands", len(all_cmds))
        for cmd in all_cmds:
            if isinstance(cmd, app_commands.Group):
                log.info("  /%s (group, subs: %s)", cmd.name, [s.name for s in cmd.commands])
            else:
                log.info("  /%s", cmd.name)

    async def on_ready(self) -> None:
        _set_bot(self)
        _set_bot_running(True)
        log.info("Bot logged in as %s (id=%s)", self.user, self.user.id)
        log.info("Bot sees %d guild(s):", len(self.guilds))
        for g in self.guilds:
            log.info("  - '%s' (id=%s)", g.name, g.id)

        # Регистрируем все guilds в БД + создаём недостающие таблицы + апрувим существующие
        import guild as guild_module
        for g in self.guilds:
            try:
                # auto_approve=True для всех guilds при on_ready — если бот на сервере, значит апрувим
                is_new = await guild_module.upsert_guild(
                    g.id, g.name,
                    icon_url=str(g.icon.url) if g.icon else None,
                    owner_id=g.owner_id,
                    member_count=g.member_count,
                    auto_approve=True,  # ← апрувим все guilds где бот присутствует
                )
                await guild_module.init_guild_tables(g.id)
                if is_new:
                    log.info("New guild: %s (id=%s) — approved", g.name, g.id)
                else:
                    log.info("Existing guild: %s (id=%s) — re-approved", g.name, g.id)
            except Exception as e:
                log.error("Failed to register guild %s: %s", g.id, e)

        all_cmds = self.tree.get_commands()
        log.info("Tree at on_ready: %d commands", len(all_cmds))

        if not all_cmds:
            log.error("⚠️ Tree is EMPTY at on_ready! Cogs failed to load.")
            return

        if not self.guilds:
            log.warning("Bot is in 0 guilds. Cache may be cold — restart in 30s.")
            try:
                synced = await self.tree.sync()
                log.info("Global sync: %d commands", len(synced))
            except Exception as e:
                log.error("Global sync failed: %s", e, exc_info=True)
            return

        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
            except Exception as e:
                log.warning("copy_global_to failed for '%s': %s", guild.name, e)
            try:
                # Полная очистка старых команд перед синхронизацией
                # Это нужно когда структура команд изменилась (например /filmnight → /filmnight start)
                self.tree.clear_commands(guild=guild)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                names = []
                for c in synced:
                    if isinstance(c, app_commands.Group):
                        names.append(f"{c.name}/({len(c.commands)} subs)")
                    else:
                        names.append(c.name)
                log.info("✓ Synced %d commands to guild '%s': %s",
                         len(synced), guild.name, names)
            except discord.Forbidden as e:
                log.error("✗ Forbidden syncing to guild '%s': %s. RE-INVITE with scope applications.commands.", guild.name, e)
            except Exception as e:
                log.error("✗ Failed to sync to guild '%s': %s", guild.name, e, exc_info=True)

        # === Проверка новой версии → пост в канал обновлений ===
        try:
            from changelog_parser import get_latest_version, get_latest_changelog
            import os

            # CHANGELOG.md может быть в корне /app (bind-mount) или рядом с кодом
            changelog_path = "CHANGELOG.md"
            if not os.path.exists(changelog_path):
                # Пробуем абсолютные пути
                for p in ["/app/CHANGELOG.md", os.path.join(os.path.dirname(__file__), "CHANGELOG.md")]:
                    if os.path.exists(p):
                        changelog_path = p
                        break

            current_version = get_latest_version(changelog_path)
            last_announced = await db.get_setting("last_announced_version")

            log.info("Version check: current=%s, last_announced=%s, changelog_path=%s, exists=%s",
                     current_version, last_announced, changelog_path, os.path.exists(changelog_path))

            if last_announced != current_version:
                log.info("New version detected: %s (was: %s) — posting update", current_version, last_announced)

                updates_channel_id_str = await db.get_setting("channel_updates_id")
                if not updates_channel_id_str or not updates_channel_id_str.isdigit():
                    log.warning("channel_updates_id not set — skipping changelog post")
                else:
                    channel = self.get_channel(int(updates_channel_id_str))
                    if channel is None:
                        log.warning("Updates channel %s not found", updates_channel_id_str)
                    else:
                        entry = get_latest_changelog(changelog_path)
                        embed = discord.Embed(
                            title=f"🐝 DeeBeelkin обновился до {current_version}!",
                            color=0xFFB703,
                            timestamp=datetime.utcnow(),
                        )

                        if entry and entry.sections:
                            for section_title, items in entry.sections.items():
                                # Фильтруем: "Техническое" НЕ попадает в анон
                                if "техн" in section_title.lower():
                                    continue
                                emoji = "🆕" if "нов" in section_title.lower() else "✅" if "испр" in section_title.lower() or "улучш" in section_title.lower() else "📋"
                                # Ограничиваем длину — Discord embed field max 1024
                                text = "\n".join(f"{emoji} {item}" for item in items[:10])
                                if len(text) > 1000:
                                    text = text[:1000] + "…"
                                embed.add_field(name=section_title, value=text, inline=False)

                        embed.set_footer(text=f"DeeBeelkin {current_version}")
                        await channel.send(embed=embed)
                        log.info("Changelog posted to channel %s", updates_channel_id_str)

                # Сохраняем текущую версию как последнюю объявленную
                await db.set_setting("last_announced_version", current_version, is_secret=False)
            else:
                log.info("Version %s already announced — skipping", current_version)
        except Exception as e:
            log.warning("Changelog post failed: %s", e, exc_info=True)

    async def on_message(self, message: discord.Message) -> None:
        """Авто-захват цитат: если сообщение в канале #цитатник от человека —
        сохраняем как цитату, удаляем исходное, постим embed.
        """
        # Пропускаем свои сообщения и сообщения других ботов
        if message.author.bot:
            return

        # Пропускаем если это не текстовый канал
        if not message.channel or not message.guild:
            return

        guild_id = message.guild.id

        # Проверяем что это канал #цитатник
        quotes_channel_id_str = await db.get_setting("channel_quotes_id")
        if not quotes_channel_id_str or not quotes_channel_id_str.isdigit():
            return
        if message.channel.id != int(quotes_channel_id_str):
            return

        # Пропускаем пустые сообщения (embed-only, attachments)
        text = message.content.strip()
        if not text:
            return

        # Сохраняем цитату в БД
        author_display = message.author.display_name or message.author.name
        author_avatar = str(message.author.display_avatar.url) if message.author.display_avatar else None
        message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

        quote_id = await db.g_add_quote(
            guild_id,
            author=author_display,
            author_user_id=message.author.id,
            text=text,
            recorded_by=message.author.id,
            author_avatar_url=author_avatar,
            message_link=message_link,
        )

        # Удаляем исходное сообщение
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        # Постим embed вместо него
        embed = discord.Embed(
            description=f"_{text}_",
            color=0xFFB703,
            timestamp=datetime.utcnow(),
        )
        if author_avatar:
            embed.set_author(name=author_display, icon_url=author_avatar)
        else:
            embed.set_author(name=author_display)
        embed.set_footer(text=f"Записал: {message.author.display_name} · #{quote_id}")

        await message.channel.send(embed=embed)

        # Опциональный TG кросс-пост
        tg_quotes_enabled = await db.get_setting("tg_crosspost_quotes")
        if tg_quotes_enabled == "1":
            tg_text = f"💬 <b>{tg_escape(author_display)}</b>\n\n<i>{tg_escape(text)}</i>"
            await tg_crosspost(tg_text)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Захват цитат реакцией: если юзер ставит настроенный эмодзи на сообщении
        в любом канале — бот сохраняет как цитату и постит embed в #цитатник.
        """
        # Пропускаем реакции от ботов
        if payload.user_id == self.user.id:
            return

        # Получаем настроенный эмодзи (по умолчанию 🗣️)
        quote_emoji = await db.get_setting("quote_emoji") or "🗣️"
        emoji_str = str(payload.emoji)

        # Сравниваем (учитываем что Discord может передавать с skin tone и т.д.)
        if emoji_str != quote_emoji:
            # Проверяем по имени (для кастомных эмодзи)
            if hasattr(payload.emoji, 'name') and payload.emoji.name != quote_emoji:
                return
            elif not hasattr(payload.emoji, 'name'):
                return

        # Получаем канал и сообщение
        channel = self.get_channel(payload.channel_id)
        if channel is None:
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        # Пропускаем сообщения ботов
        if message.author.bot:
            return

        text = message.content.strip()
        if not text:
            return  # пустое сообщение (только embed/attachment)

        guild_id = payload.guild_id or 0

        # Проверяем что канал #цитатник настроен
        quotes_channel_id_str = await db.get_setting("channel_quotes_id")
        if not quotes_channel_id_str or not quotes_channel_id_str.isdigit():
            return

        # Если реакция поставлена В канале #цитатник — не дублируем
        if message.channel.id == int(quotes_channel_id_str):
            return

        # Сохраняем цитату
        author_display = message.author.display_name or message.author.name
        author_avatar = str(message.author.display_avatar.url) if message.author.display_avatar else None
        # message.guild может быть None для DM (хотя on_raw_reaction_add с настроенным emoji в DM — редкость, но безопасно)
        guild_id_for_link = message.guild.id if message.guild else (payload.guild_id or 0)
        message_link = f"https://discord.com/channels/{guild_id_for_link}/{message.channel.id}/{message.id}"

        # Проверяем дубликат по message_link
        existing = await db.g_list_quotes(guild_id, limit=500, search=None)
        existing_links = {q[7] for q in existing if q[7]}
        if message_link in existing_links:
            # Уже сохранена — удаляем реакцию
            try:
                await message.remove_reaction(payload.emoji, payload.member or await self.fetch_user(payload.user_id))
            except (discord.Forbidden, discord.NotFound):
                pass
            return

        quote_id = await db.g_add_quote(
            guild_id,
            author=author_display,
            author_user_id=message.author.id,
            text=text,
            recorded_by=payload.user_id,  # кто поставил реакцию — тот и "записал"
            author_avatar_url=author_avatar,
            message_link=message_link,
        )

        # Постим embed в #цитатник
        quotes_channel = self.get_channel(int(quotes_channel_id_str))
        if quotes_channel:
            embed = discord.Embed(
                description=f"_{text}_",
                color=0xFFB703,
                timestamp=datetime.utcnow(),
            )
            if author_avatar:
                embed.set_author(name=author_display, icon_url=author_avatar)
            else:
                embed.set_author(name=author_display)
            # Кто поставил реакцию = кто записал
            reactor = payload.member or await self.fetch_user(payload.user_id)
            reactor_name = reactor.display_name if reactor else "Неизвестен"
            embed.set_footer(text=f"Записал: {reactor_name} · #{quote_id}")
            embed.add_field(name="Источник", value=f"[Перейти]({message_link})", inline=False)

            await quotes_channel.send(embed=embed)

        # TG кросс-пост
        tg_quotes_enabled = await db.get_setting("tg_crosspost_quotes")
        if tg_quotes_enabled == "1":
            tg_text = f"💬 <b>{tg_escape(author_display)}</b>\n\n<i>{tg_escape(text)}</i>"
            await tg_crosspost(tg_text)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Вызывается когда бота добавляют на сервер (новый или после кика).
        Если бот уже был здесь — переапрувливаем автоматически.
        """
        log.info("🎉 Bot added to guild: %s (id=%s, members=%d)", guild.name, guild.id, guild.member_count)
        try:
            import guild as guild_module
            # auto_approve=True — если бот уже был на этом сервере, переапрувливаем
            # (бота могли кикнуть и добавить обратно)
            is_new = await guild_module.upsert_guild(
                guild.id, guild.name,
                icon_url=str(guild.icon.url) if guild.icon else None,
                owner_id=guild.owner_id,
                member_count=guild.member_count,
                auto_approve=True,  # ← всегда апрувим при on_guild_join
            )
            await guild_module.init_guild_tables(guild.id)
            if is_new:
                log.info("New guild registered: %s (id=%s)", guild.name, guild.id)
            else:
                log.info("Re-approved existing guild: %s (id=%s)", guild.name, guild.id)
        except Exception as e:
            log.error("Failed to register new guild %s: %s", guild.id, e)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Вызывается когда бота удаляют с сервера.
        НЕ удаляем данные автоматически — админ может сделать это через /guilds.
        Просто помечаем как не approved.
        """
        log.info("Bot removed from guild: %s (id=%s)", guild.name, guild.id)
        try:
            import guild as guild_module
            await guild_module.reject_guild(guild.id)
            log.info("Guild %s marked as not approved (data preserved)", guild.id)
        except Exception as e:
            log.error("Failed to mark guild %s as removed: %s", guild.id, e)

    async def on_wheel_spin_completed(self, winner: dict) -> None:
        """Вызывается из web.py после завершения спина колеса.
        Отправляет embed с победителем в Discord-канал #winners (если настроен).
        Также постит в Telegram с постером и метаданными (если настроен).
        """
        log.info("Wheel spin completed, winner: %s", winner)

        # 1. Получаем метаданные (один раз для Discord + Telegram)
        meta = await self._resolve_winner_meta(winner)

        # 2. Discord пост
        winners_channel_id_str = await db.get_setting("channel_winners_id")
        if winners_channel_id_str and winners_channel_id_str.isdigit():
            channel = self.get_channel(int(winners_channel_id_str))
            if channel is not None:
                embed = self._build_winner_embed(winner, meta)
                try:
                    await channel.send(embed=embed)
                except Exception as e:
                    log.error("Failed to post winner to Discord channel: %s", e)
            else:
                log.warning("Winners channel %s not found", winners_channel_id_str)
        else:
            log.info("No channel_winners_id set, skipping Discord post")

        # 3. Telegram пост в чат (с постером и метаданными если есть)
        await self._post_winner_to_telegram(winner, meta)

        # 4. Персональные уведомления в личку организаторам + админам
        try:
            await self.notify_wheel_winner(winner, meta)
        except Exception as e:
            log.error("notify_wheel_winner failed: %s", e)

    async def _resolve_winner_meta(self, winner: dict) -> dict | None:
        """Получить метаданные фильма из БД/Кинопоиска.
        Если kp_id есть — lookup_by_id. Если нет — lookup_movie по названию.
        Возвращает meta dict или None.
        """
        from kinopoisk import lookup_by_id, lookup_movie

        kp_id = winner.get("tmdb_id")

        # Если kp_id нет — пробуем найти по названию
        if not kp_id:
            log.info("Winner '%s' has no kp_id — trying lookup_movie() by name", winner["name"])
            meta = await lookup_movie(winner["name"])
            if meta:
                kp_id = meta.get("tmdb_id")
                log.info("Found kp_id=%s for '%s' via lookup_movie", kp_id, winner["name"])
                # Обновляем запись в wheel_items
                try:
                    async with db._connect() as conn:
                        await conn.execute(
                            "UPDATE wheel_items SET tmdb_id = ? WHERE id = ?",
                            (kp_id, winner["id"]),
                        )
                        await conn.commit()
                except Exception as e:
                    log.warning("Failed to update wheel_items.tmdb_id: %s", e)
                return meta
            return None

        # Если kp_id есть — lookup_by_id
        return await lookup_by_id(kp_id)

    def _build_winner_embed(self, winner: dict, meta: dict | None) -> "discord.Embed":
        """Построить Discord embed из метаданных.
        Имя добавившего фильм раскрывается ТОЛЬКО при победе.
        Синхронный метод: не делает Discord API запросов, использует только кеш self.guilds[*].get_member(...).
        """
        embed = discord.Embed(
            title=f"🎡 Победитель колеса — {winner['name']}",
            color=0x2ECC71,
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="✓ confirmed · автоматически из веб-панели")

        # Раскрываем кто предложил фильм — только при победе
        added_by_id = winner.get("added_by")
        if added_by_id:
            # Ищем участника по всем серверам где бот присутствует (только кеш, без API запросов)
            member = None
            for g in self.guilds:
                member = g.get_member(added_by_id)
                if member is not None:
                    break
            if member:
                embed.add_field(name="🎬 Кто предложил", value=member.mention, inline=False)
            else:
                embed.add_field(name="🎬 Кто предложил", value=f"<@{added_by_id}>", inline=False)

        if meta:
            if meta.get("year"):
                embed.title = f"🎡 Победитель — {meta['title']} ({meta['year']})"
            if meta.get("poster_url"):
                embed.set_image(url=meta["poster_url"])
            if meta.get("tagline"):
                embed.description = f"_{meta['tagline']}_"
            if meta.get("plot"):
                plot = meta["plot"][:300] + "…" if len(meta["plot"]) > 300 else meta["plot"]
                embed.add_field(name="Описание", value=plot, inline=False)
            rating_str = f"⭐ {meta['vote_average']}/10"
            if meta.get("vote_count"):
                rating_str += f" ({meta['vote_count']} голосов)"
            embed.add_field(name="Рейтинг", value=rating_str, inline=True)
            if meta.get("genres"):
                embed.add_field(name="Жанры", value=", ".join(meta["genres"]), inline=True)
            if meta.get("imdb_id"):
                embed.add_field(name="IMDB", value=f"[tt{meta['imdb_id']}](https://www.imdb.com/title/tt{meta['imdb_id']}/)", inline=False)
            embed.add_field(name="Кинопоиск", value="[Источник](https://kinopoiskapiunofficial.tech/)", inline=False)
        else:
            embed.description = f"_{winner['name']}_"
            embed.add_field(name="Кинопоиск", value="Метаданные не найдены", inline=False)

        return embed

    async def _post_winner_to_telegram(self, winner: dict, meta: dict | None) -> None:
        """Пост победителя в Telegram.
        Если есть метаданные (постер) → send_photo с HTML caption + кнопкой.
        Если нет → send_message с простым текстом.
        Использует tg_winners_chat_id + tg_winners_thread_id (если заданы).
        Fallback на telegram_chat_id + telegram_thread_id.
        """
        # Выбираем чат: приоритет tg_winners_chat_id, потом telegram_chat_id
        tg_chat = await db.get_setting("tg_winners_chat_id") or await db.get_setting("telegram_chat_id")
        if not tg_chat:
            log.info("No Telegram chat configured, skipping TG post")
            return

        # thread_id: приоритет tg_winners_thread_id, потом telegram_thread_id
        tg_thread = await db.get_setting("tg_winners_thread_id") or await db.get_setting("telegram_thread_id")
        tg_thread_id = int(tg_thread) if tg_thread and tg_thread.isdigit() else None

        # Получаем токен
        from telegram import send_message, send_photo
        raw_token = await db.get_setting("telegram_token")
        if raw_token:
            tg_token = crypto.decrypt(raw_token)
        else:
            tg_token = settings.telegram_token
        if not tg_token:
            log.info("No Telegram token, skipping TG post")
            return

        # URL веб-панели для кнопки "Оценить" — из настроек (panel_base_url)
        # Если не задан — кнопку не показываем (хардкодить домен нельзя — это чужой бот у других юзеров)
        panel_url = await db.get_setting("panel_base_url")
        rate_button = None
        if panel_url:
            rate_button = {
                "inline_keyboard": [[
                    {"text": "📊 Оценить в панели", "url": f"{panel_url.rstrip('/')}/winners"},
                ]]
            }

        if meta and meta.get("poster_url"):
            # Rich post: фото + HTML caption + кнопка
            title = meta.get("title", winner["name"])
            year = meta.get("year", "")
            rating = meta.get("vote_average", 0)
            votes = meta.get("vote_count", 0)
            genres = meta.get("genres", [])
            runtime = meta.get("runtime")
            plot = meta.get("plot", "")
            imdb_id = meta.get("imdb_id")

            caption_parts = [
                "🎡 <b>Победитель колеса!</b>\n",
                f"🎬 <b>{tg_escape(title)}</b>",
            ]
            if year:
                caption_parts.append(f"({tg_escape(year)})")
            caption_parts.append("\n")

            if rating:
                rating_str = f"⭐ <b>{rating}</b>/10"
                if votes:
                    rating_str += f" ({votes:,} голосов)"
                caption_parts.append(rating_str + "\n")

            if genres:
                caption_parts.append(f"🎭 {tg_escape(', '.join(genres))}\n")

            if runtime:
                caption_parts.append(f"⏱ {runtime} мин\n")

            if plot:
                # Обрезаем чтобы уложиться в 1024 символа caption лимита
                max_plot = 500
                if len(plot) > max_plot:
                    plot = plot[:max_plot].rstrip() + "…"
                caption_parts.append(f"\n{tg_escape(plot)}\n")

            caption_parts.append(f"\n📺 <a href=\"https://kinopoisk.ru/film/{meta.get('tmdb_id', '')}/\">Кинопоиск</a>")
            if imdb_id:
                caption_parts.append(f" · <a href=\"https://www.imdb.com/title/tt{imdb_id}/\">IMDb</a>")

            caption = "".join(caption_parts)

            log.info("Posting winner to TG (photo+caption, %d chars)", len(caption))
            await send_photo(
                tg_token, tg_chat, meta["poster_url"], caption,
                thread_id=tg_thread_id, reply_markup=rate_button,
            )
        else:
            # Fallback: простой текст без постера
            text = (
                f"🎡 <b>Победитель колеса!</b>\n\n"
                f"🎬 <b>{tg_escape(winner['name'])}</b>\n"
                f"<i>Метаданные не найдены</i>"
            )
            log.info("Posting winner to TG (text only, no poster)")
            await send_message(
                tg_token, tg_chat, text,
                thread_id=tg_thread_id,
            )

    async def _notify_organizers(
        self,
        setting_key: str,
        text: str,
        photo_url: str | None = None,
        caption: str | None = None,
        reply_markup: dict | None = None,
        organizer_discord_id: int | None = None,
    ) -> int:
        """Отправить персональное уведомление в личку организаторам + админам.

        :param setting_key: ключ из TG_NOTIFY_SETTINGS ('tg_notify_winner', etc.)
        :param text: текст для sendMessage (если photo_url не задан)
        :param photo_url: URL фото (если нужно sendPhoto)
        :param caption: caption для фото (если есть photo_url)
        :param reply_markup: inline-кнопки
        :param organizer_discord_id: ID организатора сбора (если применимо)
        :return: количество успешно отправленных уведомлений

        Если у организатора/админа не привязан TG — пропускаем молча.
        Тумблер должен быть включён в /profile.
        """
        try:
            recipients = await db.get_notification_recipients(setting_key, organizer_discord_id)
        except Exception as e:
            log.error("get_notification_recipients failed: %s", e)
            return 0

        if not recipients:
            log.info("No recipients for setting_key=%s (organizer=%s)", setting_key, organizer_discord_id)
            return 0

        # Получаем TG-токен один раз
        raw_token = await db.get_setting("telegram_token")
        if raw_token:
            tg_token = crypto.decrypt(raw_token)
        else:
            tg_token = settings.telegram_token
        if not tg_token:
            log.warning("No TG token, cannot send notifications")
            return 0

        sent_count = 0
        for recipient in recipients:
            tg_user_id = str(recipient["tg_user_id"])
            try:
                if photo_url and caption:
                    success = await send_photo(
                        tg_token, tg_user_id, photo_url, caption,
                        reply_markup=reply_markup,
                    )
                else:
                    success = await send_message(
                        tg_token, tg_user_id, text,
                        reply_markup=reply_markup,
                    )
                if success:
                    sent_count += 1
                    log.info("TG notify sent to user %s (%s)", recipient["discord_id"], recipient.get("tg_username") or "no_username")
                else:
                    log.warning("TG notify failed for user %s", recipient["discord_id"])
            except Exception as e:
                log.error("TG notify exception for user %s: %s", recipient["discord_id"], e)

        return sent_count

    async def notify_wheel_winner(self, winner: dict, meta: dict | None) -> None:
        """Уведомление о победителе колеса в личку организаторам + админам.
        Текстовое сообщение с постером если есть метаданные.
        """
        # Находим started_by последнего завершённого сбора
        guild_id = 0  # TODO: когда будет multi-guild — брать из контекста
        last_collection = await db.g_get_last_completed_collection(guild_id)
        organizer_id = last_collection["started_by"] if last_collection else None

        panel_url = await db.get_setting("panel_base_url")
        rate_button = None
        if panel_url:
            rate_button = {
                "inline_keyboard": [[
                    {"text": "📊 Оценить в панели", "url": f"{panel_url.rstrip('/')}/winners"},
                ]]
            }

        if meta and meta.get("poster_url"):
            # Rich post: фото + caption
            title = meta.get("title", winner["name"])
            year = meta.get("year", "")
            rating = meta.get("vote_average", 0)
            votes = meta.get("vote_count", 0)
            genres = meta.get("genres", [])
            runtime = meta.get("runtime")
            plot = meta.get("plot", "")

            caption_parts = [
                "🎡 <b>Победитель колеса!</b>\n",
                f"🎬 <b>{tg_escape(title)}</b>",
            ]
            if year:
                caption_parts.append(f"({tg_escape(year)})")
            caption_parts.append("\n")

            if rating:
                rating_str = f"⭐ <b>{rating}</b>/10"
                if votes:
                    rating_str += f" ({votes:,} голосов)"
                caption_parts.append(rating_str + "\n")

            if genres:
                caption_parts.append(f"🎭 {tg_escape(', '.join(genres))}\n")
            if runtime:
                caption_parts.append(f"⏱ {runtime} мин\n")
            if plot:
                max_plot = 500
                if len(plot) > max_plot:
                    plot = plot[:max_plot].rstrip() + "…"
                caption_parts.append(f"\n{tg_escape(plot)}\n")

            tmdb_id = meta.get("tmdb_id")
            imdb_id = meta.get("imdb_id")
            caption_parts.append(f"\n📺 <a href=\"https://kinopoisk.ru/film/{tmdb_id or ''}/\">Кинопоиск</a>")
            if imdb_id:
                caption_parts.append(f" · <a href=\"https://www.imdb.com/title/tt{imdb_id}/\">IMDb</a>")

            caption = "".join(caption_parts)

            count = await self._notify_organizers(
                setting_key="tg_notify_winner",
                text=caption,  # fallback если photo упадёт
                photo_url=meta["poster_url"],
                caption=caption,
                reply_markup=rate_button,
                organizer_discord_id=organizer_id,
            )
            log.info("notify_wheel_winner: sent to %d recipients", count)
        else:
            # Текст без постера
            text = (
                f"🎡 <b>Победитель колеса!</b>\n\n"
                f"🎬 <b>{tg_escape(winner['name'])}</b>"
            )
            count = await self._notify_organizers(
                setting_key="tg_notify_winner",
                text=text,
                reply_markup=rate_button,
                organizer_discord_id=organizer_id,
            )
            log.info("notify_wheel_winner (no poster): sent to %d recipients", count)

    async def notify_collection_started(self, started_by: int, max_per_user: int) -> None:
        """Уведомление «Ты начал сбор фильмов, жди участников» в личку организатору.
        Также админам с включённым тумблером.
        """
        text = (
            "🎬 <b>Сбор фильмов начат!</b>\n\n"
            f"Лимит: <b>{max_per_user}</b> фильм(ов) на участника.\n"
            "Ждите, пока участники выберут фильмы и нажмут «Готов».\n\n"
            "Когда все будут готовы — придёт отдельное уведомление."
        )
        count = await self._notify_organizers(
            setting_key="tg_notify_collection_started",
            text=text,
            organizer_discord_id=started_by,
        )
        log.info("notify_collection_started: sent to %d recipients", count)

    async def notify_collection_all_ready(self, organizer_discord_id: int, ready_count: int) -> None:
        """Уведомление «Все готовы, можно начинать крутить!» в личку организатору + админам."""
        text = (
            "✅ <b>Все готовы!</b>\n\n"
            f"<b>{ready_count}</b> участник(ов) отметились готовыми.\n"
            "Можно начинать крутить колесо на веб-панели."
        )
        count = await self._notify_organizers(
            setting_key="tg_notify_collection_ready",
            text=text,
            organizer_discord_id=organizer_discord_id,
        )
        log.info("notify_collection_all_ready: sent to %d recipients", count)

    async def ping_unlinked_organizer(self, discord_user_id: int) -> None:
        """Тегнуть в Discord ephemeral что организатор не привязал TG.
        Вызывается когда уведомление не удалось отправить (нет привязки TG).
        """
        # Проверяем что юзер не привязан
        if await db.is_tg_linked(discord_user_id):
            return  # уже привязан, не надо пинговать

        # Ищем юзера в любом guild
        member = None
        for g in self.guilds:
            member = g.get_member(discord_user_id)
            if member is None:
                try:
                    member = await g.fetch_member(discord_user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            if member is not None:
                break

        if member is None:
            log.warning("Cannot ping unlinked organizer %s — member not found in any guild", discord_user_id)
            return

        try:
            await member.send(
                "ℹ️ Вы начали сбор фильмов, но ваш Telegram не привязан.\n"
                "Используйте команду `/linktg` в Discord чтобы получать персональные уведомления "
                "о готовности участников и других событиях киновечера."
            )
            log.info("Pinged unlinked organizer %s in DM", discord_user_id)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("Cannot DM user %s: %s", discord_user_id, e)



# === COG: Quotes ===

class QuotesCog(commands.Cog):
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    @app_commands.command(name="funword", description="Записать цитату в #цитатник")
    @app_commands.describe(author="Автор цитаты (@упоминание или имя)", text="Текст цитаты")
    async def funword(self, interaction: discord.Interaction, author: str, text: str):
        text = text.strip()
        author = author.strip()
        if not text or not author:
            await interaction.response.send_message("Автор и текст обязательны.", ephemeral=True)
            return

        quotes_channel_id_str = await db.get_setting("channel_quotes_id")
        if not quotes_channel_id_str or not quotes_channel_id_str.isdigit():
            await interaction.response.send_message(
                "❌ Канал #цитатник не привязан в веб-панели.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(int(quotes_channel_id_str))
        if channel is None:
            await interaction.response.send_message("❌ Канал #цитатник недоступен боту.", ephemeral=True)
            return

        # Парсинг автора: @mention или plain text
        author_display = author
        author_user_id = None
        author_member = None
        author_avatar_url = None
        if author.startswith("<@") and author.endswith(">"):
            try:
                author_user_id = int(author.strip("<@!>"))
            except ValueError:
                pass

            if author_user_id is not None and interaction.guild is not None:
                author_member = interaction.guild.get_member(author_user_id)
                if author_member is None:
                    try:
                        author_member = await interaction.guild.fetch_member(author_user_id)
                    except (discord.NotFound, discord.Forbidden):
                        pass
                if author_member is not None:
                    author_display = author_member.display_name
                    author_avatar_url = str(author_member.display_avatar.url) if author_member.display_avatar else None

        # Ссылка на исходное сообщение (если команда вызвана из канала)
        message_link = None
        if interaction.channel and interaction.guild:
            message_link = f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel.id}/{interaction.id}"

        # Сохраняем в БД с аватаром и ссылкой
        guild_id = interaction.guild_id or 0
        quote_id = await db.g_add_quote(
            guild_id, author_display, author_user_id, text,
            interaction.user.id, author_avatar_url, message_link,
        )

        # Стильный embed: цветная полоска слева, аватар автора, упоминание автора, footer
        embed = discord.Embed(
            description=f"_{text}_",
            color=0xFFB703,  # медовый, вписывается в общий стиль
            timestamp=datetime.utcnow(),
        )
        # set_author с аватаром
        if author_avatar_url:
            embed.set_author(name=author_display, icon_url=author_avatar_url)
        else:
            embed.set_author(name=author_display)

        # Поле "Автор" с упоминанием (если это был @mention)
        if author_member is not None:
            embed.add_field(name="Автор", value=author_member.mention, inline=True)

        # Footer с записавшим
        embed.set_footer(
            text=f"Записал: {interaction.user.display_name} · #{quote_id}",
            icon_url=str(interaction.user.display_avatar.url) if interaction.user.display_avatar else None,
        )

        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Цитата #{quote_id} улетела в {channel.mention}.",
            ephemeral=True,
        )

        # Опциональный TG кросс-пост
        tg_quotes_enabled = await db.get_setting("tg_crosspost_quotes")
        if tg_quotes_enabled == "1":
            tg_text = f"💬 <b>{tg_escape(author_display)}</b>\n\n<i>{tg_escape(text)}</i>"
            await tg_crosspost(tg_text)


# === COG: MovieNight ===

class MovieNightCog(commands.Cog):
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    @app_commands.command(name="movienight", description="Анонс киновечера + Scheduled Event + пинг роли")
    @app_commands.describe(
        date="Дата в формате ГГГГ-ММ-ДД (например 2025-12-31)",
        time="Время в формате ЧЧ:ММ (например 20:00), по московскому времени",
        description="Короткое описание (опционально)",
    )
    async def movienight(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str,
        description: str | None = None,
    ):
        from timezone_utils import MSK
        try:
            # Парсим как локальное время по МСК, конвертируем в UTC для хранения
            dt_msk = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
            dt = dt_msk.astimezone(timezone.utc).replace(tzinfo=None)  # naive UTC для совместимости со старым кодом
            dt_display = dt_msk  # для отображения пользователю — в МСК
        except ValueError:
            await interaction.response.send_message(
                "❌ Формат даты/времени неверный. Пример: 2025-12-31 20:00 (по МСК)",
                ephemeral=True,
            )
            return

        if dt < datetime.utcnow():
            await interaction.response.send_message("❌ Дата в прошлом. Укажи будущую дату.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False, thinking=False)

        announce_channel_id_str = await db.get_setting("channel_announce_id")
        role_id_str = await db.get_setting("role_movie_ping_id")

        guild = interaction.guild
        event = None
        if guild:
            try:
                event = await guild.create_scheduled_event(
                    name=description or "Киновечер",
                    description=description or "Собираемся смотреть фильм с колеса.",
                    start_time=dt,
                    entity_type=discord.EntityType.external,
                    location="Discord voice channel",
                    privacy_level=discord.PrivacyLevel.guild_only,
                )
            except Exception as e:
                log.warning("Failed to create scheduled event: %s", e)

        guild_id = interaction.guild_id or 0
        await db.g_add_movie_night(
            guild_id, description, dt, interaction.user.id,
            event.id if event else None,
        )

        announce_channel = None
        if announce_channel_id_str and announce_channel_id_str.isdigit():
            announce_channel = self.bot.get_channel(int(announce_channel_id_str))

        role_mention = f"<@&{role_id_str}>" if role_id_str and role_id_str.isdigit() else "@Киновечер"
        event_link = f"https://discord.com/events/{guild.id}/{event.id}" if event and guild else ""

        embed = discord.Embed(
            title=f"🎬 Киновечер — {dt_display.strftime('%d.%m.%Y %H:%M')} МСК",
            description=description or "Смотрим фильм с колеса.",
            color=0xE74C3C,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Когда", value=f"<t:{int(dt.timestamp())}:F>", inline=False)
        embed.add_field(name="Кто ведёт", value=interaction.user.mention, inline=False)
        if event_link:
            embed.add_field(name="Событие", value=f"[Открыть]({event_link})", inline=False)
        embed.set_footer(text=f"Анонсировал: {interaction.user.display_name}")

        if announce_channel:
            await announce_channel.send(content=role_mention, embed=embed, allowed_mentions=discord.AllowedMentions(roles=True))
            await interaction.followup.send(
                f"✅ Анонс киновечера опубликован в {announce_channel.mention}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(embed=embed, content=role_mention)

        tg_announce_enabled = await db.get_setting("tg_crosspost_announce")
        if tg_announce_enabled == "1":
            tg_text = (
                f"🎬 <b>Киновечер</b>\n"
                f"Когда: {dt_display.strftime('%d.%m.%Y %H:%M')} МСК\n"
                f"{tg_escape(description) if description else ''}"
            )
            await tg_crosspost(tg_text)


# === COG: Watchlist (список желаемого) ===

class WatchlistCog(commands.Cog):
    """Команды для управления личным списком желаемого."""
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    @app_commands.command(name="addfilm", description="Добавить фильм в список желаемого")
    @app_commands.describe(title="Название фильма")
    async def addfilm(self, interaction: discord.Interaction, title: str):
        from kinopoisk import lookup_movie
        title = title.strip()
        if not title:
            await interaction.response.send_message("Название не может быть пустым.", ephemeral=True)
            return

        guild_id = interaction.guild_id or 0
        import guild as guild_module
        await guild_module.init_guild_tables(guild_id)

        # Ищем метаданные в Кинопоиске
        meta = await lookup_movie(title)
        name = meta["title"] if meta else title
        tmdb_id = meta["tmdb_id"] if meta else None

        watchlist_id, error = await db.g_add_to_watchlist(guild_id, interaction.user.id, name, tmdb_id)

        if error == "already_in_other_watchlist":
            # Фильм уже в вишлисте у другого юзера — НЕ создаём дубликат
            await interaction.response.send_message(
                f"⚠️ Фильм «{name}» уже кем-то добавлен в список желаемого.\n"
                "Это сюрприз для колеса — нельзя иметь у двух людей сразу.",
                ephemeral=True,
            )
            return
        if error == "already_yours":
            await interaction.response.send_message(
                f"ℹ️ Фильм «{name}» уже в вашем списке желаемого.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="✅ Добавлено в список желаемого",
            description=f"**{name}**" + (f" ({meta['year']})" if meta and meta.get("year") else ""),
            color=0xFFB703,
        )
        if meta and meta.get("poster_url"):
            embed.set_thumbnail(url=meta["poster_url"])
        embed.set_footer(text=f"#{watchlist_id} · /myfilms — ваш список")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="delfilm", description="Удалить фильм из списка желаемого")
    @app_commands.describe(title="Название фильма (или часть)")
    async def delfilm(self, interaction: discord.Interaction, title: str):
        title = title.strip()
        guild_id = interaction.guild_id or 0

        items = await db.g_list_watchlist(guild_id, interaction.user.id, include_watched=True)
        # Ищем по частичному совпадению
        matches = [item for item in items if title.lower() in item[1].lower()]

        if not matches:
            await interaction.response.send_message(
                f"«{title}» не найден в вашем списке.",
                ephemeral=True,
            )
            return

        if len(matches) == 1:
            # Одно совпадение — удаляем
            await db.g_remove_from_watchlist(guild_id, matches[0][0], interaction.user.id)
            await interaction.response.send_message(
                f"✅ «{matches[0][1]}» удалён из списка желаемого.",
                ephemeral=True,
            )
        else:
            # Несколько — показываем список
            lines = [f"Найдено {len(matches)} фильмов. Уточните название:"]
            for i, (w_id, w_title, _, _, _) in enumerate(matches[:10], 1):
                lines.append(f"{i}. {w_title}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="myfilms", description="Показать ваш список желаемого")
    async def myfilms(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        items = await db.g_list_watchlist(guild_id, interaction.user.id, include_watched=False)

        if not items:
            await interaction.response.send_message(
                "Ваш список желаемого пуст. Используйте /addfilm чтобы добавить.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📋 Список желаемого ({len(items)})",
            color=0xFFB703,
            timestamp=datetime.utcnow(),
        )
        lines = []
        for i, (w_id, title, tmdb_id, added_at, _) in enumerate(items, 1):
            lines.append(f"{i}. **{title}**")
        embed.description = "\n".join(lines[:25])
        if len(items) > 25:
            embed.set_footer(text=f"Показано 25 из {len(items)}")
        else:
            embed.set_footer(text=f"Всего: {len(items)} фильмов")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class SettingsCog(commands.Cog):
    """Команды настройки."""
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    @app_commands.command(name="setquoteemoji", description="Установить эмодзи для захвата цитат реакцией")
    @app_commands.describe(emoji="Эмодзи (например 🗣️, 💬, ⭐, или любой другой)")
    async def setquoteemoji(self, interaction: discord.Interaction, emoji: str):
        emoji = emoji.strip()
        if not emoji:
            await interaction.response.send_message("Эмодзи не может быть пустым.", ephemeral=True)
            return

        # Сохраняем в БД (глобальная настройка)
        await db.set_setting("quote_emoji", emoji, is_secret=False)

        await interaction.response.send_message(
            f"✅ Эмодзи для захвата цитат установлен: {emoji}\n\n"
            f"Теперь любой участник может поставить {emoji} на сообщение в любом канале — "
            f"бот сохранит его как цитату и постит embed в #цитатник.",
            ephemeral=False,
        )

    @app_commands.command(name="changelog", description="Показать последнюю версию и что нового")
    async def changelog(self, interaction: discord.Interaction):
        from changelog_parser import get_latest_version, get_latest_changelog

        version = get_latest_version("CHANGELOG.md")
        entry = get_latest_changelog("CHANGELOG.md")

        embed = discord.Embed(
            title=f"🐝 DeeBeelkin {version}",
            color=0xFFB703,
            timestamp=datetime.utcnow(),
        )

        if entry and entry.sections:
            for section_title, items in entry.sections.items():
                emoji = "🆕" if "нов" in section_title.lower() else "✅" if "испр" in section_title.lower() else "📋"
                # Ограничиваем длину — Discord embed field max 1024
                text = "\n".join(f"{emoji} {item}" for item in items[:10])
                if len(text) > 1000:
                    text = text[:1000] + "…"
                embed.add_field(name=section_title, value=text, inline=False)
        else:
            embed.description = "Changelog не найден."

        embed.set_footer(text=f"DeeBeelkin {version}")
        await interaction.response.send_message(embed=embed, ephemeral=False)


# === COG: TG Link ===

class LinkCog(commands.Cog):
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    @app_commands.command(name="linktg", description="Сгенерировать код для привязки Telegram-аккаунта")
    async def linktg(self, interaction: discord.Interaction):
        # Проверяем, не привязан ли уже
        existing = await db.get_tg_link(interaction.user.id)
        if existing and existing[5]:  # linked_at
            await interaction.response.send_message(
                f"✓ Ваш Telegram уже привязан: @{existing[2] or 'без username'}.\n"
                "Если хотите перепривязать — попросите админа сбросить связь в панели.",
                ephemeral=True,
            )
            return

        # Генерируем 6-значный код
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        expires_at = datetime.utcnow() + timedelta(minutes=10)

        await db.create_tg_link_code(interaction.user.id, code, expires_at)

        # Проверяем что TG-бот настроен
        tg_token, _ = await get_tg_config()
        if not tg_token:
            await interaction.response.send_message(
                "❌ TG-бот не настроен администратором. Невозможно привязать аккаунт.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🔑 Ваш код привязки: **`{code}`**\n\n"
            "Инструкция:\n"
            "1. Найдите бота в Telegram (имя бота уточните у админа)\n"
            "2. Отправьте боту команду `/start <код>` или просто `<код>`\n"
            "3. Код действителен 10 минут\n\n"
            "После привязки вы будете получать персональные уведомления в TG.",
            ephemeral=True,
        )


# === Обработка входящих сообщений в TG (для /linktg кода) ===
# Это делается через long-polling в отдельной таске — добавляется в main.py.
# Реализация — в tg_bot.py (см. ниже).

async def handle_tg_link_message(text: str, tg_user_id: int, tg_username: str | None) -> str | None:
    """Если text — это код привязки, пытаемся привязать. Возвращает ответное сообщение или None."""
    text = text.strip()
    if not text:
        return None

    # Принимаем "/start CODE" или просто "CODE"
    if text.startswith("/start "):
        code = text[7:].strip()
    elif text.startswith("/start"):
        return ("Привет! Я бот киновечеров. Чтобы привязать аккаунт, "
                "отправьте код из команды /linktg в Discord.")
    elif len(text) == 6 and text.isdigit():
        code = text
    else:
        return None

    discord_id = await db.verify_tg_link(code, tg_user_id, tg_username)
    if discord_id is not None:
        return (f"✅ <b>Аккаунт привязан!</b>\n\n"
                f"Теперь вы можете включить уведомления в личке на странице профиля:\n"
                f"🌐 https://dibilkis.bothost.tech/profile\n\n"
                f"Доступные уведомления (включаются в профиле):\n"
                f"• 🎡 Победитель колеса\n"
                f"• 🎬 Сбор фильмов начат\n"
                f"• ✅ Все участники готовы\n\n"
                f"<i>По умолчанию все уведомления выключены — зайдите в профиль и включите нужные.</i>\n\n"
                f"Ваш Discord ID: <code>{discord_id}</code>")
    else:
        return "❌ Код недействителен или истёк. Сгенерируйте новый через /linktg в Discord."
