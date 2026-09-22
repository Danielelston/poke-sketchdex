"""Read-only stats commands: profile, leaderboard, streak, stats."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import desc, func, select

from .. import db, leveling

log = logging.getLogger(__name__)


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="profile", description="Show a sketcher's level, EXP, and streak.")
    @app_commands.describe(user="Whose profile to show (default: you)")
    async def profile(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        async with db.session() as s:
            row = (
                await s.execute(
                    select(db.User).where(
                        db.User.guild_id == interaction.guild_id, db.User.user_id == target.id
                    )
                )
            ).scalar_one_or_none()
            global_row = (
                await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == target.id))
            ).scalar_one_or_none()
            sub_count = (
                await s.execute(
                    select(func.count(db.Submission.id)).where(
                        db.Submission.guild_id == interaction.guild_id,
                        db.Submission.user_id == target.id,
                    )
                )
            ).scalar_one()
            global_sub_count = (
                await s.execute(
                    select(func.count(db.Submission.id)).where(
                        db.Submission.user_id == target.id,
                    )
                )
            ).scalar_one()
        if row is None:
            await interaction.response.send_message(
                f"{target.display_name} hasn't submitted any sketches yet.", ephemeral=True
            )
            return

        def _bar(exp: int) -> tuple[int, int, int, str]:
            lvl, into, need = leveling.exp_into_level(exp)
            bar_len = 12
            filled = 0 if need == 0 else int(bar_len * into / need)
            bar = "█" * filled + "░" * (bar_len - filled)
            return lvl, into, need, bar

        lvl, into, need, bar = _bar(row.exp)

        embed = discord.Embed(title=f"🏆 {target.display_name}'s PokeSketchDex Profile", color=0x5865F2)
        embed.add_field(
            name="── This Server ──",
            value=(
                f"Level {lvl}\n"
                f"{into} / {need} EXP\n"
                f"`{bar}`\n"
                f"🔥 {row.personal_streak}-day streak (server) (best {row.longest_streak})\n"
                f"{sub_count} sketches here"
            ),
            inline=True,
        )

        if global_row is None:
            # Shouldn't normally happen post-backfill — brand new user mid-first-submission.
            global_text = "No global stats yet."
        else:
            glvl, ginto, gneed, gbar = _bar(global_row.exp)
            global_text = (
                f"Level {glvl}\n"
                f"{ginto} / {gneed} EXP\n"
                f"`{gbar}`\n"
                f"🔥 {global_row.global_streak}-day streak (global) (best {global_row.longest_global_streak})\n"
                f"{global_sub_count} sketches total"
            )
        embed.add_field(name="── Global (All Servers) ──", value=global_text, inline=True)

        if isinstance(target, discord.User) and target.display_avatar:
            embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

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
