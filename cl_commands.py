"""Slash-команды ChaosLedger для Discord.

Все команды доступны ТОЛЬКО роли «Матка» (superuser) — это явно заявлено
пользователем: «Доступ только для юзера панели с ролью "Матка"». Позже
функционал для всех будет добавлен отдельно.

Имена команд с дефисом (`cl-link`, `cl-balance`, ...) — Discord допускает
lowercase + hyphens в slash-именах. Префикс `cl-` группирует команды в
autocomplete Discord'а.

Все ответы ephemeral — даже если Матка, баланс/история это личные данные.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import cl_client
import cl_data
import db
from cl_client import CLApiError, CLAuthError, CLConfigError, CLUnavailableError

log = logging.getLogger(__name__)


def _fmt_money(money: dict | None) -> str:
    """{'amount': 150.0, 'currency': 'CC'} → '150.0 CC'."""
    if not money:
        return "—"
    amt = money.get("amount", 0)
    cur = money.get("currency", "")
    # целые числа без десятичных
    if isinstance(amt, (int, float)) and amt == int(amt):
        amt = int(amt)
    return f"**{amt}** {cur}".strip()


def _fmt_time(ms: int | None) -> str:
    """epoch ms → 'YYYY-MM-DD HH:MM' (UTC)."""
    if not ms:
        return "—"
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


class ChaosLedgerCog(commands.Cog):
    """v3.13: ChaosLedger slash-команды. Доступ только для Матки (superuser)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --- хелперы доступа ----------------------------------------------------

    async def _is_matka(self, interaction: discord.Interaction) -> bool:
        """Проверить superuser. Отправляет ephemeral отказ если нет."""
        role = await db.get_user_role(interaction.user.id)
        if role != "superuser":
            await interaction.response.send_message(
                "⛔ Доступ к ChaosLedger только для роли **Матка**.",
                ephemeral=True,
            )
            return False
        return True

    async def _client_or_respond(
        self, interaction: discord.Interaction, *, defer: bool = True
    ) -> cl_client.CLClient | None:
        """Возвращает CLClient или отвечает ошибкой config. Defer'ит если нужно."""
        guild_id = interaction.guild_id or 0
        if defer and not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        client = await cl_client.get_client(guild_id)
        if not client.configured:
            await interaction.followup.send(
                "⚠️ ChaosLedger не настроен для этого сервера.\n"
                "Настройте API URL и ключ в панели: **Настройки → ChaosLedger**.",
                ephemeral=True,
            )
            return None
        return client

    async def _resolve_uuid(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> str | None:
        """MC UUID для interaction.user или member. None если не linked."""
        guild_id = interaction.guild_id or 0
        target_id = (member.id if member else interaction.user.id)
        link = await cl_data.get_link(guild_id, target_id)
        return link["mc_uuid"] if link else None

    @staticmethod
    def _err_msg(e: Exception) -> str:
        if isinstance(e, CLAuthError):
            return "❌ Неверный X-Api-Key. Проверьте config ChaosLedger."
        if isinstance(e, CLUnavailableError):
            return "❌ Мод ChaosLedger недоступен (timeout/connection)."
        if isinstance(e, CLConfigError):
            return "⚠️ ChaosLedger не настроен."
        if isinstance(e, CLApiError):
            return f"❌ Мод: {e}"
        return f"❌ {e}"

    # === /cl-link ===========================================================

    @app_commands.command(name="cl-link", description="Привязать ваш Discord к Minecraft нику (ChaosLedger)")
    @app_commands.describe(minecraft_nick="Ваш ник в Minecraft (Java Edition)")
    async def cl_link(self, interaction: discord.Interaction, minecraft_nick: str) -> None:
        if not await self._is_matka(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        nick = minecraft_nick.strip()
        try:
            resolved = await cl_data.resolve_mc_uuid(nick)
        except RuntimeError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if not resolved:
            await interaction.followup.send(
                f"❌ Игрок `{nick}` не найден в Mojang.", ephemeral=True
            )
            return
        mc_uuid, mc_name = resolved
        guild_id = interaction.guild_id or 0
        await cl_data.link_player(guild_id, interaction.user.id, mc_uuid, mc_name)
        await interaction.followup.send(
            f"✅ Привязка сохранена: **{interaction.user.display_name}** → "
            f"`{mc_name}` (`{mc_uuid}`).",
            ephemeral=True,
        )

    # === /cl-unlink =========================================================

    @app_commands.command(name="cl-unlink", description="Отвязать ваш Discord от Minecraft (ChaosLedger)")
    async def cl_unlink(self, interaction: discord.Interaction) -> None:
        if not await self._is_matka(interaction):
            return
        guild_id = interaction.guild_id or 0
        removed = await cl_data.unlink_player(guild_id, interaction.user.id)
        if removed:
            await interaction.response.send_message("✅ Привязка удалена.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "ℹ️ У вас не было привязки.", ephemeral=True
            )

    # === /cl-balance ========================================================

    @app_commands.command(name="cl-balance", description="Баланс ChaosLedger (свой или @user)")
    @app_commands.describe(
        user="Чей баланс показать (по умолчанию свой)",
        currency="Код валюты (по умолчанию CC)",
    )
    async def cl_balance(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        currency: str = "CC",
    ) -> None:
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        mc_uuid = await self._resolve_uuid(interaction, user)
        if not mc_uuid:
            await interaction.followup.send(
                "ℹ️ Discord не привязан к Minecraft. Сначала `/cl-link <ник>`.",
                ephemeral=True,
            )
            return
        try:
            acc = await client.get_account(mc_uuid, currency)
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        name = user.display_name if user else interaction.user.display_name
        await interaction.followup.send(
            f"💰 **{name}** · `{currency}`: {_fmt_money(acc.get('balance'))}",
            ephemeral=True,
        )

    # === /cl-pay ============================================================

    @app_commands.command(name="cl-pay", description="Перевести деньги другому игроку (ChaosLedger)")
    @app_commands.describe(
        to="Кому перевести (@участник с привязанной MC)",
        amount="Сумма",
        currency="Код валюты (по умолчанию CC)",
        reason="Комментарий (опционально)",
    )
    async def cl_pay(
        self,
        interaction: discord.Interaction,
        to: discord.Member,
        amount: float,
        currency: str = "CC",
        reason: str = "",
    ) -> None:
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        if amount <= 0:
            await interaction.followup.send("❌ Сумма должна быть положительной.", ephemeral=True)
            return
        from_uuid = await self._resolve_uuid(interaction)
        to_uuid = await self._resolve_uuid(interaction, to)
        if not from_uuid:
            await interaction.followup.send("ℹ️ Вы не привязаны. `/cl-link <ник>`.", ephemeral=True)
            return
        if not to_uuid:
            await interaction.followup.send(
                f"ℹ️ {to.mention} не привязан к Minecraft.", ephemeral=True
            )
            return
        try:
            txn = await client.pay({
                "fromUuid": from_uuid,
                "fromName": interaction.user.display_name,
                "toUuid": to_uuid,
                "toName": to.display_name,
                "currency": currency,
                "amount": amount,
                "reason": reason or "discord_pay",
            })
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Переведено {_fmt_money(txn.get('amount'))} → {to.mention} "
            f"(txn #{txn.get('id', '?')}).",
            ephemeral=True,
        )

    # === /cl-top ============================================================

    @app_commands.command(name="cl-top", description="Топ игроков по балансу (ChaosLedger)")
    @app_commands.describe(currency="Код валюты (по умолчанию CC)", limit="Сколько (1-20, по умолчанию 10)")
    async def cl_top(
        self, interaction: discord.Interaction, currency: str = "CC", limit: int = 10
    ) -> None:
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        limit = max(1, min(20, limit))
        try:
            top = await client.get_top(currency, limit=limit)
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        if not top:
            await interaction.followup.send("ℹ️ Нет счетов.", ephemeral=True)
            return
        lines = [f"🏆 **Топ по `{currency}`**:"]
        for i, acc in enumerate(top, 1):
            name = acc.get("ownerName", "?")
            bal = _fmt_money(acc.get("balance"))
            lines.append(f"`{i:>2}`. **{name}** — {bal}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # === /cl-quests =========================================================

    @app_commands.command(name="cl-quests", description="Список квестов ChaosLedger")
    async def cl_quests(self, interaction: discord.Interaction) -> None:
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        try:
            quests = await client.get_quests()
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        if not quests:
            await interaction.followup.send("ℹ️ Квестов нет.", ephemeral=True)
            return
        lines = ["📜 **Квесты ChaosLedger**:"]
        for q in quests[:20]:
            reward = q.get("reward", {}) or {}
            rtype = reward.get("type", "?")
            rtxt = (
                _fmt_money(reward.get("money")) if rtype == "MONEY"
                else f"{reward.get('count', 0)}× {reward.get('itemId', '?')}"
            )
            rep = "🔁" if q.get("repeatable") else "1️⃣"
            lines.append(f"{rep} `{q.get('id', '?')}` — **{q.get('title', '?')}** → {rtxt}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # === /cl-quest ==========================================================

    @app_commands.command(name="cl-quest", description="Детали одного квеста (ChaosLedger)")
    @app_commands.describe(quest_id="ID квеста")
    async def cl_quest(self, interaction: discord.Interaction, quest_id: str) -> None:
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        try:
            q = await client.get_quest(quest_id)
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        obj_lines = []
        for o in q.get("objectives", []) or []:
            obj_lines.append(f"  • `{o.get('id')}` [{o.get('type')}] {o.get('description', '')} (нужно {o.get('required', 0)})")
        reward = q.get("reward", {}) or {}
        rtype = reward.get("type", "?")
        rtxt = (
            _fmt_money(reward.get("money")) if rtype == "MONEY"
            else f"{reward.get('count', 0)}× {reward.get('itemId', '?')}"
        )
        msg = (
            f"📜 **{q.get('title', '?')}** (`{q.get('id')}`)\n"
            f"{q.get('description', '')}\n\n"
            f"**Цели:**\n" + "\n".join(obj_lines) + "\n\n"
            f"**Награда:** {rtxt}\n"
            f"Повторяемый: {'да' if q.get('repeatable') else 'нет'} · "
            f"cooldown: {q.get('cooldownSec', 0)}с"
        )
        await interaction.followup.send(msg[:1900], ephemeral=True)

    # === /cl-market =========================================================

    @app_commands.command(name="cl-market", description="Рынок ChaosLedger — все предметы")
    async def cl_market(self, interaction: discord.Interaction) -> None:
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        try:
            items = await client.get_market()
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        if not items:
            await interaction.followup.send("ℹ️ Рынок пуст.", ephemeral=True)
            return
        lines = ["🏪 **Рынок ChaosLedger**:"]
        for it in items[:15]:
            last = it.get("lastPrice", {}) or {}
            base = it.get("basePrice", {}) or {}
            lines.append(
                f"• `{it.get('itemId', '?')}` — {it.get('displayName', '?')} · "
                f"цена {_fmt_money(last)} (base {_fmt_money(base)}) · "
                f"stock {it.get('stock', 0)}"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # === /cl-buy / /cl-sell =================================================

    async def _trade(
        self, interaction: discord.Interaction, item_id: str, quantity: float, *, buy: bool
    ) -> None:
        verb = "Купить" if buy else "Продать"
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        if quantity <= 0:
            await interaction.followup.send("❌ Количество должно быть положительным.", ephemeral=True)
            return
        mc_uuid = await self._resolve_uuid(interaction)
        if not mc_uuid:
            await interaction.followup.send("ℹ️ Вы не привязаны. `/cl-link <ник>`.", ephemeral=True)
            return
        req = {
            "playerUuid": mc_uuid,
            "playerName": interaction.user.display_name,
            "itemId": item_id,
            "quantity": quantity,
        }
        try:
            item = await (client.market_buy if buy else client.market_sell)(req)
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ {verb}: **{quantity}** × `{item_id}` по {_fmt_money(item.get('lastPrice'))}.",
            ephemeral=True,
        )

    @app_commands.command(name="cl-buy", description="Купить предмет на рынке (ChaosLedger)")
    @app_commands.describe(item_id="ID предмета (например minecraft:diamond)", quantity="Сколько")
    async def cl_buy(
        self, interaction: discord.Interaction, item_id: str, quantity: float
    ) -> None:
        await self._trade(interaction, item_id, quantity, buy=True)

    @app_commands.command(name="cl-sell", description="Продать предмет на рынке (ChaosLedger)")
    @app_commands.describe(item_id="ID предмета (например minecraft:diamond)", quantity="Сколько")
    async def cl_sell(
        self, interaction: discord.Interaction, item_id: str, quantity: float
    ) -> None:
        await self._trade(interaction, item_id, quantity, buy=False)

    # === /cl-history ========================================================

    @app_commands.command(name="cl-history", description="История транзакций ChaosLedger (своих или @user)")
    @app_commands.describe(
        user="Чья история (по умолчанию своя)",
        limit="Сколько записей (1-20, по умолчанию 10)",
    )
    async def cl_history(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None,
        limit: int = 10,
    ) -> None:
        if not await self._is_matka(interaction):
            return
        client = await self._client_or_respond(interaction)
        if not client:
            return
        mc_uuid = await self._resolve_uuid(interaction, user)
        if not mc_uuid:
            await interaction.followup.send("ℹ️ Не привязан. `/cl-link <ник>`.", ephemeral=True)
            return
        limit = max(1, min(20, limit))
        try:
            txns = await client.get_player_transactions(mc_uuid, limit=limit)
        except Exception as e:
            await interaction.followup.send(self._err_msg(e), ephemeral=True)
            return
        if not txns:
            await interaction.followup.send("ℹ️ История пуста.", ephemeral=True)
            return
        lines = [f"📋 **История** ({user.display_name if user else interaction.user.display_name}):"]
        for t in txns[:limit]:
            cat = t.get("category", "?")
            amt = _fmt_money(t.get("amount"))
            reason = t.get("reason") or ""
            ts = _fmt_time(t.get("createdAt"))
            lines.append(f"• [{cat}] {amt} · {reason} · {ts}")
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ChaosLedgerCog(bot))
