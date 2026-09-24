"""`/profile-card`: a generated Pillow player profile card — username, Poké
Ball rank badge (corner emblem + full-bleed watermark), EXP/streak/submission
stats, and active party sprites. Additive to the existing text-embed
`/profile` (cogs/stats.py), which stays unchanged.

Public by default (not ephemeral): unlike `/pokebox`/`/catch` (personal
utility, ephemeral), this is a shareable "flex" image — same visibility as
`/profile` and `/party`, which this feature is a visual sibling of.
"""

from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from .. import cards, db, leveling, pokebox, rank_badges

log = logging.getLogger(__name__)


class ProfileCard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="profile-card",
        description="Generate a shareable profile card: level, Poké Ball rank, stats, and party.",
    )
    @app_commands.describe(user="Whose profile card to generate (default: you)")
    async def profile_card(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        # Rendering is fast in practice, but a cold PokeAPI sprite fetch on a
        # cache miss could still push this past Discord's 3s ack window —
        # defer proactively rather than reactively (this project was already
        # bitten once by an interaction-ack-adjacent bug, commit 4bc0ae9).
        await interaction.response.defer(thinking=True)

        target = user or interaction.user
        async with db.session() as s:
            global_row = (
                await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == target.id))
            ).scalar_one_or_none()
            sub_count = (
                await s.execute(
                    select(func.count(db.Submission.id)).where(db.Submission.user_id == target.id)
                )
            ).scalar_one()
            party = await pokebox.party_listing(s, target.id)

        if global_row is None:
            await interaction.followup.send(
                f"{target.display_name} hasn't submitted any sketches yet.", ephemeral=True
            )
            return

        level, exp_current, exp_needed = leveling.exp_into_level(global_row.exp)
        rank_tier = rank_badges.rank_for_level(level)

        corner_path, full_bleed_path = await rank_badges.get_tier_assets(
            rank_tier, cards.CARD_SIZE, self.bot.config.badge_cache_dir
        )

        sprite_paths = []
        for mon in party:
            path = await self.bot.api.get_sprite_image_path(mon.dex_no, shiny=mon.is_shiny)
            if path:
                sprite_paths.append(path)

        png_bytes = await asyncio.to_thread(
            cards.render_profile_card,
            target.display_name,
            level,
            rank_tier,
            exp_current,
            exp_needed,
            global_row.global_streak,
            sub_count,
            sprite_paths,
            corner_path,
            full_bleed_path,
        )

        await interaction.followup.send(
            file=discord.File(io.BytesIO(png_bytes), filename="profile-card.png")
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProfileCard(bot))
