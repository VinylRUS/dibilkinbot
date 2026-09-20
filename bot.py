"""Discord-бот: slash-команды /wheel, /watched, /funword, /movienight, /linktg + авто-detect победителей."""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

import crypto
import db
import kinopoisk as tmdb
from kinopoisk import lookup_by_id
import telegram
from config import settings
from telegram import send_message, tg_escape

log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = False
intents.guilds = True
intents.members = True  # нужен для проверки что юзер — участник сервера при логине в панель


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
            ("WheelCog", WheelCog),
            ("QuotesCog", QuotesCog),
            ("MovieNightCog", MovieNightCog),
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

        # Регистрируем все guilds в БД (создаём таблицы если ещё не созданы)
        import guild as guild_module
        for g in self.guilds:
            try:
                is_new = await guild_module.upsert_guild(
                    g.id, g.name,
                    icon_url=str(g.icon.url) if g.icon else None,
                    owner_id=g.owner_id,
                    member_count=g.member_count,
                )
                if is_new:
                    log.info("New guild detected: %s (id=%s) — creating tables, pending approval", g.name, g.id)
                    await guild_module.init_guild_tables(g.id)
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

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Вызывается когда бота добавляют на новый сервер.
        Создаёт guild-таблицы, регистрирует сервер в реестре (pending approval).
        """
        log.info("🎉 Bot added to guild: %s (id=%s, members=%d)", guild.name, guild.id, guild.member_count)
        try:
            import guild as guild_module
            is_new = await guild_module.upsert_guild(
                guild.id, guild.name,
                icon_url=str(guild.icon.url) if guild.icon else None,
                owner_id=guild.owner_id,
                member_count=guild.member_count,
            )
            if is_new:
                log.info("New guild — creating tables, marking pending approval")
                await guild_module.init_guild_tables(guild.id)
                # Уведомление в лог — админ увидит и аппрувнет через /guilds
                log.warning("⚠️ Guild %s (id=%s) is pending admin approval. Use /guilds in web panel to approve.",
                            guild.name, guild.id)
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

        # 3. Telegram пост (с постером и метаданными если есть)
        await self._post_winner_to_telegram(winner, meta)

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
        """Построить Discord embed из метаданных."""
        embed = discord.Embed(
            title=f"🎡 Победитель колеса — {winner['name']}",
            color=0x2ECC71,
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text="✓ confirmed · автоматически из веб-панели")

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

        # URL веб-панели для кнопки "Оценить"
        panel_url = await db.get_setting("panel_base_url") or "https://your-panel-domain"
        rate_button = {
            "inline_keyboard": [[
                {"text": "📊 Оценить в панели", "url": f"{panel_url}/winners"},
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



# === COG: Wheel ===

class WheelCog(commands.Cog):
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    wheel = app_commands.Group(name="wheel", description="Колесо фильмов")

    @wheel.command(name="add", description="Добавить фильм в колесо + метаданные из Кинопоиска")
    @app_commands.describe(title="Название фильма (русское или оригинальное)")
    async def wheel_add(self, interaction: discord.Interaction, title: str):
        title = title.strip()
        if not title:
            await interaction.response.send_message("Название не может быть пустым.", ephemeral=True)
            return

        guild_id = interaction.guild_id or 0
        if await db.g_is_watched(guild_id, title):
            await interaction.response.send_message(
                f"«{title}» уже просмотрен — его нельзя вернуть в колесо.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Ищем метаданные в Кинопоиске
        meta = await tmdb.lookup_movie(title)

        # Если нашли метаданные — используем локализованный title для колеса
        name_for_wheel = meta["title"] if meta else title
        tmdb_id = meta["tmdb_id"] if meta else None

        # Добавляем в локальную БД
        item_id = await db.g_add_wheel_item(guild_id, name_for_wheel, tmdb_id, interaction.user.id)

        # Broadcast через WebSocket (если кто-то смотрит /wheel страницу)
        try:
            import ws_manager
            items = await db.g_list_wheel_items(guild_id, active_only=True)
            await ws_manager.broadcast_wheel_updated(items)
        except Exception as e:
            log.debug("WS broadcast failed: %s", e)

        # Строим ответ с метаданными если есть
        if meta:
            embed = discord.Embed(
                title=f"✅ «{meta['title']}» добавлен в колесо",
                color=0x2ECC71,
                timestamp=datetime.utcnow(),
            )
            if meta.get("original_title") and meta["original_title"] != meta["title"]:
                embed.add_field(name="Оригинал", value=meta["original_title"], inline=True)
            if meta.get("year"):
                embed.add_field(name="Год", value=meta["year"], inline=True)
            if meta.get("vote_average"):
                embed.add_field(name="Рейтинг", value=f"⭐ {meta['vote_average']}/10", inline=True)
            if meta.get("genres"):
                embed.add_field(name="Жанры", value=", ".join(meta["genres"]), inline=False)
            if meta.get("plot"):
                plot = meta["plot"][:300] + "…" if len(meta["plot"]) > 300 else meta["plot"]
                embed.add_field(name="Описание", value=plot, inline=False)
            if meta.get("poster_url"):
                embed.set_thumbnail(url=meta["poster_url"])
            embed.set_footer(text=f"Веб-панель: /wheel · Кинопоиск ID: {meta['tmdb_id']} · *uses kinopoisk.dev API*")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                f"✅ «{title}» добавлен в колесо.\n"
                "_Кинопоиск метаданные недоступны — задайте токен в панели, либо фильм не найден._\n"
                "Открыть колесо: /wheel в веб-панели",
                ephemeral=True,
            )

    @wheel.command(name="list", description="Показать текущие пункты колеса")
    async def wheel_list(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id or 0
        items = await db.g_list_wheel_items(guild_id, active_only=True)
        if not items:
            await interaction.response.send_message(
                "Колесо пустое. Добавь через `/wheel add <название>` или через веб-панель /wheel",
                ephemeral=True,
            )
            return

        lines = [f"**Колесо** ({len(items)} шт.):"]
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. **{item['name']}**")
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n... (обрезано)"
        await interaction.response.send_message(
            text + "\n\nКрутить: /wheel в веб-панели",
            ephemeral=True,
        )

    @wheel.command(name="remove", description="Удалить фильм из колеса (по номеру из /wheel list)")
    @app_commands.describe(number="Номер фильма в списке /wheel list")
    async def wheel_remove(self, interaction: discord.Interaction, number: int):
        guild_id = interaction.guild_id or 0
        items = await db.g_list_wheel_items(guild_id, active_only=True)
        if number < 1 or number > len(items):
            await interaction.response.send_message(
                f"Номер должен быть от 1 до {len(items)}",
                ephemeral=True,
            )
            return
        item = items[number - 1]
        await db.g_remove_wheel_item(guild_id, item["id"])
        await db.g_reassign_colors(guild_id)
        try:
            import ws_manager
            updated = await db.g_list_wheel_items(guild_id, active_only=True)
            await ws_manager.broadcast_wheel_updated(updated)
        except Exception:
            pass
        await interaction.response.send_message(
            f"✅ «{item['name']}» удалён из колеса.",
            ephemeral=True,
        )

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
        time="Время в формате ЧЧ:ММ (например 20:00), часовой пояс UTC",
        description="Короткое описание (опционально)",
    )
    async def movienight(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str,
        description: str | None = None,
    ):
        try:
            dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        except ValueError:
            await interaction.response.send_message(
                "❌ Формат даты/времени неверный. Пример: 2025-12-31 20:00",
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
            title=f"🎬 Киновечер — {dt.strftime('%d.%m.%Y %H:%M UTC')}",
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
                f"Когда: {dt.strftime('%d.%m.%Y %H:%M UTC')}\n"
                f"{tg_escape(description) if description else ''}"
            )
            await tg_crosspost(tg_text)


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
        return (f"✅ Аккаунт привязан!\n"
                f"Теперь вы будете получать персональные уведомления о киновечерах "
                f"и победителях колеса.\n\nВаш Discord ID: {discord_id}")
    else:
        return "❌ Код недействителен или истёк. Сгенерируйте новый через /linktg в Discord."
