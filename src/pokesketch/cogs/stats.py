"""Read-only stats commands: leaderboard, streak, stats.

`/profile` used to live here as a separate text-embed command, overlapping
with `/profile-card`'s Pillow-rendered card — the two were merged into one
native-embed `/profile` command (cogs/profile.py) per the design doc's
"Command consolidation" decision. This cog keeps the other, unrelated
stats commands.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import desc, select

from .. import db, leveling

log = logging.getLogger(__name__)


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Top sketchers by EXP.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(db.User)
                    .where(db.User.guild_id == interaction.guild_id)
                    .order_by(desc(db.User.exp))
                    .limit(10)
                )
            ).scalars().all()
        if not rows:
            await interaction.response.send_message("No sketchers yet — be the first!", ephemeral=True)
            return
        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = []
        for i, r in enumerate(rows):
            lvl = leveling.level_for_exp(r.exp)
            lines.append(f"{medals[i]} <@{r.user_id}> — Lv{lvl} · {r.exp} EXP · {r.personal_streak}🔥")
        embed = discord.Embed(
            title="🏆 PokeSketchDex Leaderboard",
            description="\n".join(lines),
            color=0xF1C40F,
        )
        await interaction.response.send_message(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="streak", description="Show the server's running streak and totals.")
    async def streak(self, interaction: discord.Interaction) -> None:
        await self._send_server_stats(interaction)

    @app_commands.command(name="stats", description="Alias for /streak.")
    async def stats(self, interaction: discord.Interaction) -> None:
        await self._send_server_stats(interaction)

    async def _send_server_stats(self, interaction: discord.Interaction) -> None:
        async with db.session() as s:
            stats = await s.get(db.GuildStats, interaction.guild_id)
        if stats is None:
            await interaction.response.send_message("No activity yet.", ephemeral=True)
            return
        embed = discord.Embed(title="📈 Server stats", color=0x2ECC71)
        embed.add_field(name="Current streak", value=f"{stats.global_streak} days 🔥")
        embed.add_field(name="Longest streak", value=f"{stats.longest_global_streak} days")
        embed.add_field(name="Total sketches", value=str(stats.total_submissions))
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Stats(bot))
