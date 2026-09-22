"""`/help` command: player + admin reference, including how EXP is earned."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import leveling


def _exp_field_value() -> str:
    """Build the EXP-earning explainer from the live leveling constants.

    Sourced directly from `leveling.py` so this never drifts out of sync
    with the actual award logic in `cogs/submissions.py`.

    Maintenance: when adding/renaming/removing a slash command, or when
    tuning EXP rules in `leveling.py`, also update the command-list text
    below (player/admin sections) and this EXP explainer — see the
    "Maintenance rule" callout in the Obsidian Bot Plan doc.
    """
    return (
        f"• **Submit a sketch:** +{leveling.EXP_SUBMIT} EXP (once per day, per daily thread)\n"
        f"• **Keep your streak going:** +{leveling.EXP_PER_STREAK_DAY} EXP per consecutive day, "
        f"capped at +{leveling.EXP_STREAK_CAP}\n"
        f"• **Get upvoted (👍 on your sketch):** +{leveling.EXP_PER_UPVOTE} EXP per upvote, "
        f"capped at +{leveling.EXP_UPVOTE_DAILY_CAP}/day\n\n"
        "EXP counts both **per-server** and **globally** (your global total is the sum "
        "across every server you sketch in) — see `/profile`."
    )


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="How PokeSketchDex works, EXP, and the full command list.")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        is_admin = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_guild
        )

        embed = discord.Embed(
            title="📖 PokeSketchDex Help",
            description=(
                "Every day the bot posts a Pokémon reference and pings the sketcher role. "
                "Sketch it, post your art in that day's thread with `/submit`, and react 👍 "
                "on entries you like."
            ),
            color=0x5865F2,
        )
        embed.add_field(name="⭐ How EXP works", value=_exp_field_value(), inline=False)
        embed.add_field(
            name="🧑‍🎨 Player commands",
            value=(
                "`/submit` — submit your sketch in today's thread (or an older day's thread)\n"
                "`/profile [user]` — your (or someone's) level, EXP, and streaks\n"
                "`/leaderboard` — top sketchers in this server by EXP\n"
                "`/streak` / `/stats` — server-wide streak and totals\n"
                "`/pokebox [user]` — dex completion tracker (free, automatic on `/submit`)\n"
                "`/catch` — spend a pokeball to catch today's just-submitted sketch\n"
                "`/party [user]` — your 6 active party slots\n"
                "`/box [user]` — paginated view of your storage box\n"
                "`/swap <box_slot> <active_slot>` — swap a boxed mon into your active party\n"
                "`/release <slot> [active]` — release a caught mon and free its slot\n"
                "`/pokeballs [user]` — your pokeball balance and next weekly grant\n"
                "`/help` — this message"
            ),
            inline=False,
        )
        admin_value = (
            "`/setup` — set the post channel, role, time, and timezone\n"
            "`/set-mode` — random vs. no-repeat-until-exhausted selection\n"
            "`/set-generations` — restrict the dex range\n"
            "`/pause` / `/resume` — pause or resume daily posts\n"
            "`/post-now` — post today's challenge immediately\n"
            "`/reset-pool` — manually clear the no-repeat pool (confirmation required)\n"
            "\nAll require the **Manage Server** permission."
        )
        if not is_admin:
            admin_value += "\n-# You don't have Manage Server here, so these aren't available to you."
        embed.add_field(name="🛠️ Admin commands", value=admin_value, inline=False)
        embed.set_footer(text="PokeSketchDex • one Pokemon a day")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
