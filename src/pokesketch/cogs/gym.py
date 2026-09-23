"""`/gym` commands: admin-run Gym Events with an HP pool and player badges."""

from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import desc, func, select

from .. import db, gym, pokebox
from ..discord_limits import guarded_add_field

log = logging.getLogger(__name__)

HP_BAR_LEN = 20
MIN_HP_TOTAL = 1
MAX_HP_TOTAL = 1_000_000
MIN_DURATION_DAYS = 1
MAX_DURATION_DAYS = 90
NAME_MAX_LEN = 128
BADGE_NAME_MAX_LEN = 128
TOP_CONTRIBUTORS_LIMIT = 5
MEDALS = ["🥇", "🥈", "🥉", "🔹", "🔹"]


def _hp_bar(hp_remaining: int, hp_total: int) -> str:
    if hp_total <= 0:
        return ""
    filled = min(HP_BAR_LEN, int(HP_BAR_LEN * max(hp_remaining, 0) / hp_total))
    return "🟩" * filled + "⬜" * (HP_BAR_LEN - filled)


def _hp_field_value(event: db.GymEvent) -> str:
    return f"{_hp_bar(event.hp_remaining, event.hp_total)}\n{max(event.hp_remaining, 0)} / {event.hp_total} HP"


def _start_announcement_embed(event: db.GymEvent) -> discord.Embed:
    ends_ts = pokebox.discord_timestamp(event.ends_at, style="R")
    embed = discord.Embed(
        title=f"⚔️ Gym Event: {event.name}",
        description=(
            f"**Leader {event.leader_name}** has appeared with **{event.hp_total} HP**!\n"
            "Submit sketches, give upvotes, and get upvoted to chip away at their HP. "
            f"Defeat them before {ends_ts} to earn the **{event.badge_name}** badge!"
        ),
        color=0xE74C3C,
    )
    guarded_add_field(embed, "HP", _hp_field_value(event))
    guarded_add_field(embed, "Ends", ends_ts, inline=True)
    return embed


async def _top_contributors(s, gym_event_id: int, limit: int) -> list[tuple[int, int]]:
    rows = (
        await s.execute(
            select(db.GymContribution.user_id, func.sum(db.GymContribution.damage).label("total_damage"))
            .where(db.GymContribution.gym_event_id == gym_event_id)
            .group_by(db.GymContribution.user_id)
            .order_by(desc("total_damage"))
            .limit(limit)
        )
    ).all()
    return [(user_id, total_damage) for user_id, total_damage in rows]


class Gym(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    gym_group = app_commands.Group(name="gym", description="Gym Events: community goals with an HP pool.")

    @gym_group.command(name="start", description="Start a new Gym Event.")
    @app_commands.describe(
        name="Gym event name, e.g. \"Brock's Rock Gym Challenge\"",
        leader_name="Flavor name for the gym leader, e.g. \"Brock\"",
        hp_total="Total HP pool for the gym leader",
        duration_days="How many days the event runs before it expires",
        badge_name="Badge every contributor earns if the gym is defeated, e.g. \"Boulder Badge\"",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gym_start(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, NAME_MAX_LEN],
        leader_name: app_commands.Range[str, 1, NAME_MAX_LEN],
        hp_total: app_commands.Range[int, MIN_HP_TOTAL, MAX_HP_TOTAL],
        duration_days: app_commands.Range[int, MIN_DURATION_DAYS, MAX_DURATION_DAYS],
        badge_name: app_commands.Range[str, 1, BADGE_NAME_MAX_LEN],
    ) -> None:
        async with db.session() as s:
            existing = await gym.get_active_gym_event(s, interaction.guild_id)
            if existing is not None:
                await interaction.response.send_message(
                    f"❌ **{existing.name}** is still active. End the current gym event first with `/gym end`.",
                    ephemeral=True,
                )
                return
            now = gym.naive_utcnow()
            event = db.GymEvent(
                guild_id=interaction.guild_id,
                name=name,
                leader_name=leader_name,
                hp_total=hp_total,
                hp_remaining=hp_total,
                starts_at=now,
                ends_at=now + timedelta(days=duration_days),
                status=gym.STATUS_ACTIVE,
                badge_name=badge_name,
                channel_id=interaction.channel_id,
                created_by=interaction.user.id,
            )
            s.add(event)
            await s.commit()
        await interaction.response.send_message(embed=_start_announcement_embed(event))

    @gym_group.command(name="status", description="Show the current Gym Event's HP, time left, and top contributors.")
    async def gym_status(self, interaction: discord.Interaction) -> None:
        async with db.session() as s:
            event = await gym.get_active_gym_event(s, interaction.guild_id)
            if event is None:
                await interaction.response.send_message("No active gym event right now.", ephemeral=True)
                return
            top = await _top_contributors(s, event.id, TOP_CONTRIBUTORS_LIMIT)

        embed = discord.Embed(title=f"⚔️ {event.name}", description=f"Leader: **{event.leader_name}**", color=0xE74C3C)
        guarded_add_field(embed, "HP", _hp_field_value(event))
        guarded_add_field(embed, "Ends", pokebox.discord_timestamp(event.ends_at, style="R"), inline=True)
        if top:
            lines = [f"{MEDALS[i]} <@{uid}> — {dmg} dmg" for i, (uid, dmg) in enumerate(top)]
            guarded_add_field(embed, "Top contributors", "\n".join(lines))
        else:
            guarded_add_field(embed, "Top contributors", "No contributions yet — be the first!")
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @gym_group.command(name="end", description="Manually end the current Gym Event early.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def gym_end(self, interaction: discord.Interaction) -> None:
        async with db.session() as s:
            event = await gym.get_active_gym_event(s, interaction.guild_id)
            if event is None:
                await interaction.response.send_message("No active gym event to end.", ephemeral=True)
                return
            if event.hp_remaining <= 0:
                # Edge case: HP already hit 0 but the defeat transition never
                # ran (e.g. a crash between the decrement and the status flip).
                await gym.mark_defeated(s, event)
                await s.commit()
                await interaction.response.send_message(
                    f"🏆 **{event.name}** had already fallen — badges awarded to every contributor!"
                )
                return
            event.status = gym.STATUS_CANCELLED
            await s.commit()
        await interaction.response.send_message(f"🛑 **{event.name}** has been ended early — no badges awarded.")

    @gym_group.command(name="badges", description="List a player's earned Gym badges.")
    @app_commands.describe(user="Whose badges to show (default: you)")
    async def gym_badges(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(db.GymBadge, db.GymEvent)
                    .join(db.GymEvent, db.GymBadge.gym_event_id == db.GymEvent.id)
                    .where(db.GymBadge.user_id == target.id, db.GymEvent.guild_id == interaction.guild_id)
                    .order_by(desc(db.GymBadge.awarded_at))
                )
            ).all()
        if not rows:
            await interaction.response.send_message(
                f"{target.display_name} hasn't earned any gym badges yet.", ephemeral=True
            )
            return
        lines = [
            f"🎖️ **{event.badge_name}** — defeated {event.leader_name} ({event.name})" for _badge, event in rows
        ]
        embed = discord.Embed(
            title=f"🎖️ {target.display_name}'s Gym Badges", description="\n".join(lines), color=0xF1C40F
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Gym(bot))
