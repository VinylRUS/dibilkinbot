"""Discord-бот со slash-командами: /wheel, /watched, /funword, /movienight."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import crypto
import db
from config import settings
from pointauc import PointaucClient, PointaucError
from telegram import send_message

log = logging.getLogger("bot")

intents = discord.Intents.default()
intents.message_content = False
intents.guilds = True
# members не нужен — закрытый сервер, модерации нет


# === Токены: приоритет из БД (зашифрованы), fallback на env ===

async def get_token(key: str, env_value: str | None) -> str | None:
    raw = await db.get_setting(key)
    if raw:
        return crypto.decrypt(raw)
    return env_value


# === Бот ===

class KinovecherBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",  # не используется, только slash
            intents=intents,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        """Регистрирует cog'и с явным логированием ошибок каждой."""
        cogs = [
            ("WheelCog", WheelCog),
            ("WatchedCog", WatchedCog),
            ("QuotesCog", QuotesCog),
            ("MovieNightCog", MovieNightCog),
        ]
        for name, cls in cogs:
            try:
                await self.add_cog(cls(self))
                log.info("✓ Cog loaded: %s", name)
            except Exception as e:
                log.error("✗ Failed to load cog %s: %r", name, e, exc_info=True)

        # Логируем состояние дерева сразу после загрузки
        all_cmds = self.tree.get_commands()
        log.info("Tree after setup_hook: %d commands", len(all_cmds))
        for cmd in all_cmds:
            if isinstance(cmd, app_commands.Group):
                log.info("  /%s (group, subs: %s)", cmd.name, [s.name for s in cmd.commands])
            else:
                log.info("  /%s", cmd.name)

    async def on_ready(self) -> None:
        log.info("Bot logged in as %s (id=%s)", self.user, self.user.id)
        log.info("Bot sees %d guild(s):", len(self.guilds))
        for g in self.guilds:
            log.info("  - '%s' (id=%s)", g.name, g.id)

        # Дублируем логирование дерева — на случай если on_ready сработал раньше, чем закончился setup_hook
        all_cmds = self.tree.get_commands()
        log.info("Tree at on_ready: %d commands", len(all_cmds))

        if not all_cmds:
            log.error(
                "⚠️ Tree is EMPTY at on_ready! Cogs failed to load. "
                "See '✗ Failed to load cog' errors above."
            )
            return

        if not self.guilds:
            log.warning("Bot is in 0 guilds. Cache may be cold — restart in 30s.")
            try:
                synced = await self.tree.sync()
                log.info("Global sync: %d commands (may take up to 1 hour to appear)", len(synced))
            except Exception as e:
                log.error("Global sync failed: %s", e, exc_info=True)
            return

        for guild in self.guilds:
            # 1. Сначала копируем глобальные команды в guild-специфичные
            try:
                self.tree.copy_global_to(guild=guild)
                log.info("copy_global_to OK for guild '%s'", guild.name)
            except Exception as e:
                log.warning("copy_global_to failed for '%s': %s", guild.name, e)

            # 2. Синхронизируем
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
                log.error(
                    "✗ Forbidden syncing to guild '%s' (id=%s): %s. "
                    "RE-INVITE bot with scopes: bot + applications.commands "
                    "(Discord Developer Portal → OAuth2 → URL Generator).",
                    guild.name, guild.id, e,
                )
            except Exception as e:
                log.error("✗ Failed to sync to guild '%s' (id=%s): %s",
                          guild.name, guild.id, e, exc_info=True)


# === Помощники ===

async def get_pointauc_client() -> PointaucClient | None:
    token = await get_token("pointauc_token", settings.pointauc_token)
    if not token:
        return None
    return PointaucClient(token)


async def get_tg_config() -> tuple[str | None, str | None]:
    tg_token = await get_token("telegram_token", settings.telegram_token)
    tg_chat = await db.get_setting("telegram_chat_id") or settings.telegram_chat_id
    return tg_token, tg_chat


async def tg_crosspost(text: str) -> None:
    """Отправить сообщение в TG-бэклог. Молча пропускает если не настроено."""
    tg_token, tg_chat = await get_tg_config()
    if not tg_token or not tg_chat:
        log.debug("TG not configured, skipping crosspost")
        return
    await send_message(tg_token, tg_chat, text)


# === COG: Wheel ===

class WheelCog(commands.Cog):
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    wheel = app_commands.Group(name="wheel", description="Колесо фильмов (Pointauc)")

    @wheel.command(name="add", description="Добавить фильм в колесо Pointauc")
    @app_commands.describe(title="Название фильма")
    async def wheel_add(self, interaction: discord.Interaction, title: str):
        title = title.strip()
        if not title:
            await interaction.response.send_message("Название не может быть пустым.", ephemeral=True)
            return

        # Не добавлять просмотренные
        if await db.is_watched(title):
            await interaction.response.send_message(
                f"«{title}» уже просмотрен — его нельзя вернуть в колесо.",
                ephemeral=True,
            )
            return

        client = await get_pointauc_client()
        if client is None:
            await interaction.response.send_message(
                "❌ Pointauc token не настроен. Укажите его в веб-панели.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            bid_ids = await client.add_bid(title, cost=0)
        except PointaucError as e:
            await interaction.followup.send(f"⚠️ Pointauc error: {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ «{title}» добавлен в колесо.\nBid ID: `{bid_ids[0] if bid_ids else '—'}`\n"
            "Крутка — на pointauc.com. Победителя зафиксируй через `/watched`.",
            ephemeral=True,
        )

    @wheel.command(name="list", description="Показать текущие пункты колеса (Pointauc)")
    async def wheel_list(self, interaction: discord.Interaction):
        client = await get_pointauc_client()
        if client is None:
            await interaction.response.send_message(
                "❌ Pointauc token не настроен в веб-панели.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            lots = await client.list_lots()
        except PointaucError as e:
            await interaction.followup.send(f"⚠️ Pointauc error: {e}", ephemeral=True)
            return

        if not lots:
            await interaction.followup.send("Колесо пустое.", ephemeral=True)
            return

        lines = [f"**Колесо Pointauc** ({len(lots)} шт.):"]
        for i, lot in enumerate(lots, 1):
            name = lot.get("name") or lot.get("Name") or "—"
            amount = lot.get("amount") or lot.get("Amount") or 0
            lines.append(f"{i}. **{name}** — сумма: {amount}")

        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n... (обрезано)"
        await interaction.followup.send(text, ephemeral=True)


# === COG: Watched ===

class WatchedCog(commands.Cog):
    def __init__(self, bot: KinovecherBot):
        self.bot = bot

    @app_commands.command(name="watched", description="Пометить фильм просмотренным + пост в TG-бэклог")
    @app_commands.describe(
        title="Название фильма (точно как в колесе)",
        rating="Оценка 1-10 (опционально)",
    )
    async def watched(self, interaction: discord.Interaction, title: str, rating: int | None = None):
        title = title.strip()
        if rating is not None and not (1 <= rating <= 10):
            await interaction.response.send_message("Оценка должна быть 1-10.", ephemeral=True)
            return

        added = await db.add_watched(title, rating, interaction.user.id)
        if not added:
            await interaction.response.send_message(
                f"«{title}» уже помечен просмотренным ранее.",
                ephemeral=True,
            )
            return

        # Пост в TG
        stars = "⭐" * rating if rating else "—"
        tg_text = (
            f"🎬 <b>{title}</b>\n"
            f"Дата: {datetime.utcnow().strftime('%Y-%m-%d')}\n"
            f"Оценка: {stars}"
        )
        await tg_crosspost(tg_text)

        await interaction.response.send_message(
            f"✅ «{title}» добавлен в бэклог просмотренного. "
            f"{'Оценка: ' + stars if rating else 'Без оценки.'}",
            ephemeral=False,  # команда публичная — сервер видит
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

        # Канал цитатника из настроек
        quotes_channel_id_str = await db.get_setting("channel_quotes_id")
        if not quotes_channel_id_str or not quotes_channel_id_str.isdigit():
            await interaction.response.send_message(
                "❌ Канал #цитатник не привязан в веб-панели.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(int(quotes_channel_id_str))
        if channel is None:
            await interaction.response.send_message(
                "❌ Канал #цитатник недоступен боту.",
                ephemeral=True,
            )
            return

        # Если author — это <@ID>, достаём Member из гильдии, берём display_name.
        # Если не Member — оставляем как plain text (кто-то мог передать имя текстом).
        author_display = author  # что пойдёт в embed и БД
        author_user_id = None
        author_member = None
        if author.startswith("<@") and author.endswith(">"):
            try:
                author_user_id = int(author.strip("<@!>"))
            except ValueError:
                pass

            if author_user_id is not None and interaction.guild is not None:
                author_member = interaction.guild.get_member(author_user_id)
                if author_member is not None:
                    author_display = author_member.display_name
                else:
                    # Member не в кеше — попробуем fetch через API (медленнее, но точно)
                    try:
                        author_member = await interaction.guild.fetch_member(author_user_id)
                        author_display = author_member.display_name
                    except (discord.NotFound, discord.Forbidden):
                        # Не вышло — оставляем ID как fallback, но не сырой <@...>
                        author_display = f"User {author_user_id}"

        quote_id = await db.add_quote(author_display, author_user_id, text, interaction.user.id)

        # Embed: упоминание записавшего в footer (через display_name),
        # автор в set_author (display_name участника или исходный текст),
        # плюс отдельное поле с реальным mention автора — Discord парсит mentions в field values.
        embed = discord.Embed(
            description=f"_{text}_",
            color=0xF1C40F,
            timestamp=datetime.utcnow(),
        )
        embed.set_author(name=author_display)
        if author_member is not None:
            # Кликабельная ссылка на профиль автора (внешний jump-URL не работает для пользователей,
            # но mention в поле — парсится)
            embed.add_field(
                name="Автор",
                value=author_member.mention,
                inline=True,
            )
        embed.set_footer(text=f"Записал: {interaction.user.display_name} · #{quote_id}")

        await channel.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Цитата #{quote_id} улетела в {channel.mention}.",
            ephemeral=True,
        )

        # Опциональный кросс-пост в TG
        tg_quotes_enabled = await db.get_setting("tg_crosspost_quotes")
        if tg_quotes_enabled == "1":
            tg_text = f"💬 <b>{author}</b>\n\n<i>{text}</i>"
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
        # Парсим дату/время
        try:
            dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        except ValueError:
            await interaction.response.send_message(
                "❌ Формат даты/времени неверный. Пример: 2025-12-31 20:00",
                ephemeral=True,
            )
            return

        if dt < datetime.utcnow():
            await interaction.response.send_message(
                "❌ Дата в прошлом. Укажи будущую дату.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=False)

        # Канал анонсов + роль для пинга
        announce_channel_id_str = await db.get_setting("channel_announce_id")
        role_id_str = await db.get_setting("role_movie_ping_id")

        # Создаём Scheduled Event
        guild = interaction.guild
        event = None
        if guild:
            try:
                event = await guild.create_scheduled_event(
                    name=description or "Киновечер",
                    description=description or "Собираемся смотреть фильм с колеса Pointauc.",
                    start_time=dt,
                    entity_type=discord.EntityType.external,
                    location="Discord voice channel",
                    privacy_level=discord.PrivacyLevel.guild_only,
                )
            except Exception as e:
                log.warning("Failed to create scheduled event: %s", e)

        # Записываем в БД
        await db.add_movie_night(
            title=description,
            scheduled_at=dt,
            created_by=interaction.user.id,
            event_id=event.id if event else None,
        )

        # Постим анонс
        announce_channel = None
        if announce_channel_id_str and announce_channel_id_str.isdigit():
            announce_channel = self.bot.get_channel(int(announce_channel_id_str))

        role_mention = f"<@&{role_id_str}>" if role_id_str and role_id_str.isdigit() else "@Киновечер"
        event_link = f"https://discord.com/events/{guild.id}/{event.id}" if event and guild else ""

        embed = discord.Embed(
            title=f"🎬 Киновечер — {dt.strftime('%d.%m.%Y %H:%M UTC')}",
            description=description or "Смотрим фильм с колеса Pointauc.",
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

        # Опциональный кросс-пост в TG
        tg_announce_enabled = await db.get_setting("tg_crosspost_announce")
        if tg_announce_enabled == "1":
            tg_text = (
                f"🎬 <b>Киновечер</b>\n"
                f"Когда: {dt.strftime('%d.%m.%Y %H:%M UTC')}\n"
                f"{description or ''}"
            )
            await tg_crosspost(tg_text)
