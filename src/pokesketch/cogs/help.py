"""`/help` command: player + admin reference, including how EXP is earned."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import leveling
from ..discord_limits import DISCORD_EMBED_FIELD_LIMIT  # noqa: F401 — re-exported, see scripts/smoke_test.py
from ..discord_limits import guarded_add_field as _add_guarded_field


def _exp_field_value() -> str:
    """Build the EXP-earning explainer from the live leveling constants.

    Sourced directly from `leveling.py` so this never drifts out of sync
    with the actual award logic in `cogs/submissions.py`.

    Maintenance: when adding/renaming/removing a slash command, or when
    tuning EXP rules in `leveling.py`, also update the command-list text
    below (player section), `_admin_field_groups()` below (admin section),
    and this EXP explainer — both `/help` and `/help-admin` need updating
    together — see the "Maintenance rule" callout in the Obsidian Bot Plan doc.
    """
    return (
        f"• **Submit a sketch:** +{leveling.EXP_SUBMIT} EXP (once per day, per daily thread)\n"
        f"• **Submit to a wild encounter:** +{leveling.EXP_WILD_ENCOUNTER} EXP (once per day, per "
        "wild-encounter thread)\n"
        f"• **Keep your streak going:** +{leveling.EXP_PER_STREAK_DAY} EXP per consecutive day, "
        f"capped at +{leveling.EXP_STREAK_CAP} — submitting to the daily **and** a wild encounter "
        "the same day still only counts once toward your streak\n"
        f"• **Get upvoted (👍 on your sketch):** +{leveling.EXP_PER_UPVOTE_RECEIVED} EXP per upvote, "
        f"capped at +{leveling.EXP_UPVOTE_RECEIVED_DAILY_CAP}/day\n"
        f"• **Give an upvote (👍 someone else's sketch):** +{leveling.EXP_PER_UPVOTE_GIVEN} EXP per upvote, "
        f"capped at +{leveling.EXP_UPVOTE_GIVEN_DAILY_CAP}/day\n\n"
        "EXP counts both **per-server** and **globally** (your global total is the sum "
        "across every server you sketch in) — see `/profile`."
    )


def _admin_field_groups() -> list[tuple[str, str]]:
    """Admin command reference as (field name, field value) groups.

    Each group must stay under `DISCORD_EMBED_FIELD_LIMIT` chars — split
    further if a new command pushes a group over.
    """
    return [
        (
            "⚙️ Setup & scheduling",
            "`/setup` — set the post channel, role, time, and timezone\n"
            "`/set-mode` — random vs. no-repeat-until-exhausted selection\n"
            "`/set-generations` — restrict the dex range\n"
            "`/pause` / `/resume` — pause or resume daily posts\n"
            "`/post-now` — post today's challenge immediately",
        ),
        (
            "⏳ Grace, catch & resets",
            "`/set-grace-period` — days a thread stays open for `/submit` backfill (default 7, 1-30)\n"
            "`/set-catch-window` — hours a submission stays catchable via `/catch` (default 24, "
            "can't exceed the grace period)\n"
            "`/reset-pool` — manually clear the no-repeat pool (confirmation required)\n\n"
            "-# Note: raising the grace period past 7 days only affects the `/submit` cutoff — "
            "Discord's visible thread auto-archive tier still caps at 7 days, but archived threads "
            "auto-unarchive the moment `/submit` posts into them, so this is cosmetic, not a hard block.",
        ),
        (
            "🗳️ Weekly vote & events",
            "`/set-vote-day` — weekday the weekly wild-encounter category vote posts "
            "(default Sunday; the specific-choice vote is always the next day)\n"
            "`/event-create` — author a wild-encounter Event (explicit dex number list, optional flavor text)\n"
            "`/event-list` — paginated list of this server's Events\n"
            "`/event-view` — inspect one Event's status, Pokemon list, and flavor text\n"
            "`/event-edit` — rename an Event or replace its dex list / flavor text\n"
            "`/event-disable` / `/event-enable` — retire or restore an Event without deleting it\n\n"
            "Every server starts pre-seeded with 10 built-in Events (type-based + curated theme "
            "weeks) — see `/event-list`.\n\n"
            "All require the **Manage Server** permission (`/event-list` and `/event-view` are "
            "viewable by anyone).",
        ),
        (
            "⚔️ Gym Events",
            "`/gym start` — launch a Gym Event: a named leader with an HP pool that submissions "
            "and upvotes chip away at over a set duration\n"
            "`/gym end` — manually end the current gym event early (no badges if HP > 0)\n\n"
            "Only one gym event can be active per server at a time. See `/gym status` / "
            "`/gym badges` in `/help` for the player-facing side.",
        ),
    ]


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="How PokeSketchDex works, EXP, and the full command list.")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="📖 PokeSketchDex Help",
            description=(
                "Every day the bot posts a Pokémon reference and pings the sketcher role. "
                "Sketch it, post your art in that day's thread with `/submit`, and react 👍 "
                "on entries you like.\n\n"
                "Each week, players also vote in two stages (category, then a specific choice) "
                "for a wild-encounter theme. Once that's decided, a fresh wild-encounter thread "
                "gets posted daily alongside the main challenge — its own Pokémon, its own thread "
                "each day. `/submit` works there too."
            ),
            color=0x5865F2,
        )
        _add_guarded_field(embed, "⭐ How EXP works", _exp_field_value())
        _add_guarded_field(
            embed,
            "🧑‍🎨 Sketching & progress",
            "`/submit` — submit your sketch in today's thread (or an older day's thread)\n"
            "`/profile [user]` — your (or someone's) level, Poké Ball rank, stats, active "
            "party, and kudos — with buttons to inspect the party or give kudos\n"
            "`/leaderboard` — top sketchers in this server by EXP\n"
            "`/streak` / `/stats` — server-wide streak and totals\n"
            "`/pokebox [user]` — dex completion tracker: every sketch you `/submit` scans that Pokémon in\n"
            "`/help` — this message",
        )
        _add_guarded_field(
            embed,
            "📦 Catch & collection",
            "`/catch [target] [nickname]` — spend a pokeball to catch an eligible sketch into "
            "your party/storage; pick from the autocomplete list or leave blank for your most "
            "recent one\n"
            "-# Caught images are stored downsized (max 1080x1080px) to keep storage bounded\n"
            "`/party [user]` — your 6 active party slots\n"
            "`/box [user]` — paginated view of your storage box\n"
            "`/swap <box_slot> <active_slot>` — swap a boxed mon into your active party\n"
            "`/release <slot> [active]` — release a caught mon and free its slot\n"
            "`/rename <mon> <nickname>` — rename (or clear the nickname of) a caught mon\n"
            "`/pokeballs [user]` — your pokeball balance and next weekly grant",
        )
        _add_guarded_field(
            embed,
            "⏳ Submit vs. catch windows",
            "Submitting and catching run on separate clocks — a thread often stays open "
            "for `/submit` well after a sketch's own catch window has closed. Check a "
            "thread's opening post or your `/submit` confirmation for the current windows.",
        )
        _add_guarded_field(
            embed,
            "⚔️ Gym Events",
            "`/gym status` — current gym leader's HP, time remaining, and top contributors\n"
            "`/gym badges [user]` — your (or someone's) earned gym badges\n"
            "-# Submitting, giving upvotes, and getting upvoted (capped) all chip away at the "
            "gym leader's HP while a gym event is active — see `/help-admin` for `/gym start`.",
        )
        _add_guarded_field(embed, "🛠️ Server admin?", "See `/help-admin` for setup and moderation commands.")
        embed.set_footer(text="PokeSketchDex • one Pokemon a day")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="help-admin",
        description="Admin command reference (informational — anyone can view it).",
    )
    async def help_admin_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🛠️ PokeSketchDex Admin Commands",
            description=(
                "Informational only — running these still requires the **Manage Server** "
                "permission on this server."
            ),
            color=0x5865F2,
        )
        for name, value in _admin_field_groups():
            _add_guarded_field(embed, name, value)
        embed.set_footer(text="PokeSketchDex • one Pokemon a day")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
