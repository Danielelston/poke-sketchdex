"""Admin slash commands: setup, mode, pause/resume, post-now."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from .. import db
from ..daily import post_daily_for_guild
from ..selection import clear_used_pool
from ..ui import ConfirmView

log = logging.getLogger(__name__)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_ALL_TZ = available_timezones()

GRACE_PERIOD_MIN_DAYS = 1
GRACE_PERIOD_MAX_DAYS = 30

MIN_DEX_NO = 1
MAX_DEX_NO = 1025

WEEKDAY_CHOICES = [
    app_commands.Choice(name="Monday", value=0),
    app_commands.Choice(name="Tuesday", value=1),
    app_commands.Choice(name="Wednesday", value=2),
    app_commands.Choice(name="Thursday", value=3),
    app_commands.Choice(name="Friday", value=4),
    app_commands.Choice(name="Saturday", value=5),
    app_commands.Choice(name="Sunday", value=6),
]


def _parse_dex_numbers(raw: str) -> list[int]:
    """Parse a free-form space/comma-separated dex number list for
    /event-create. Raises ValueError with a user-facing message on any bad
    token, out-of-range number, or an empty result."""
    tokens = [t for t in re.split(r"[,\s]+", raw.strip()) if t]
    if not tokens:
        raise ValueError("Provide at least one dex number.")
    seen: list[int] = []
    for tok in tokens:
        if not tok.isdigit():
            raise ValueError(f"`{tok}` isn't a valid dex number.")
        n = int(tok)
        if not (MIN_DEX_NO <= n <= MAX_DEX_NO):
            raise ValueError(f"`{tok}` is out of range (must be {MIN_DEX_NO}-{MAX_DEX_NO}).")
        if n not in seen:
            seen.append(n)
    return seen


def _validate_grace_period(days: int) -> bool:
    return GRACE_PERIOD_MIN_DAYS <= days <= GRACE_PERIOD_MAX_DAYS


def _validate_catch_window(hours: int, grace_period_days: int) -> bool:
    return 1 <= hours <= grace_period_days * 24


def _clamp_catch_window(catch_window_hours: int, grace_period_days: int) -> int:
    return min(catch_window_hours, grace_period_days * 24)


async def _get_or_create_cfg(guild_id: int) -> db.GuildConfig:
    async with db.session() as s:
        cfg = await s.get(db.GuildConfig, guild_id)
        if cfg is None:
            cfg = db.GuildConfig(guild_id=guild_id)
            s.add(cfg)
            await s.commit()
    return cfg


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Configure the daily post channel, role, and time.")
    @app_commands.describe(
        channel="Channel to post the daily challenge in",
        role="Role to ping each day",
        time="Local post time, 24h HH:MM (e.g. 09:00)",
        timezone="IANA timezone, e.g. America/New_York",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
        time: str = "09:00",
        timezone: str = "UTC",
    ) -> None:
        if not TIME_RE.match(time):
            await interaction.response.send_message(
                "Time must be 24h HH:MM, e.g. `09:00`.", ephemeral=True
            )
            return
        if timezone not in _ALL_TZ:
            await interaction.response.send_message(
                f"Unknown timezone `{timezone}`. Use an IANA name like `America/New_York`.",
                ephemeral=True,
            )
            return
        gid = interaction.guild_id
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, gid)
            if cfg is None:
                cfg = db.GuildConfig(guild_id=gid)
                s.add(cfg)
            cfg.channel_id = channel.id
            cfg.role_id = role.id
            cfg.post_time = time
            cfg.timezone = timezone
            vote_day1_weekday = cfg.vote_day1_weekday
            await s.commit()
            if await s.get(db.GuildStats, gid) is None:
                s.add(db.GuildStats(guild_id=gid))
                await s.commit()
        self.bot.reschedule_guild(gid, time, timezone)
        self.bot.reschedule_guild_votes(gid, vote_day1_weekday, time, timezone)
        await interaction.response.send_message(
            f"✅ Daily posts set in {channel.mention}, pinging {role.mention} at "
            f"**{time} {timezone}**.",
            ephemeral=True,
        )

    @app_commands.command(name="set-mode", description="Set the Pokemon selection mode.")
    @app_commands.describe(mode="How the daily Pokemon is chosen")
    @app_commands.choices(mode=[
        app_commands.Choice(name="No repeats until pool exhausted", value="no_repeat"),
        app_commands.Choice(name="Pure random (repeats allowed)", value="random"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, interaction.guild_id)
            if cfg is None:
                await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
                return
            cfg.selection_mode = mode.value
            await s.commit()
        await interaction.response.send_message(f"Selection mode set to **{mode.name}**.", ephemeral=True)

    @app_commands.command(name="set-generations", description="Restrict the dex range (national dex numbers).")
    @app_commands.describe(dex_min="Lowest dex number (>=1)", dex_max="Highest dex number (<=1025)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_generations(self, interaction: discord.Interaction, dex_min: int, dex_max: int) -> None:
        if not (1 <= dex_min <= dex_max <= 1025):
            await interaction.response.send_message(
                "Need 1 <= min <= max <= 1025.", ephemeral=True
            )
            return
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, interaction.guild_id)
            if cfg is None:
                await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
                return
            cfg.dex_min, cfg.dex_max = dex_min, dex_max
            await s.commit()
        await interaction.response.send_message(
            f"Dex range set to #{dex_min}–#{dex_max}.", ephemeral=True
        )

    @app_commands.command(
        name="set-grace-period",
        description="Set how many days back a thread stays open for /submit backfill.",
    )
    @app_commands.describe(
        days=f"Days back a thread accepts /submit ({GRACE_PERIOD_MIN_DAYS}-{GRACE_PERIOD_MAX_DAYS}, default 7)"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_grace_period(self, interaction: discord.Interaction, days: int) -> None:
        if not _validate_grace_period(days):
            await interaction.response.send_message(
                f"Grace period must be {GRACE_PERIOD_MIN_DAYS}-{GRACE_PERIOD_MAX_DAYS} days.",
                ephemeral=True,
            )
            return
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, interaction.guild_id)
            if cfg is None:
                await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
                return
            cfg.grace_period_days = days
            clamp_note = ""
            clamped = _clamp_catch_window(cfg.catch_window_hours, days)
            if clamped != cfg.catch_window_hours:
                cfg.catch_window_hours = clamped
                clamp_note = (
                    f" Catch window was also clamped down to **{clamped}h** to stay within "
                    "the new grace period."
                )
            await s.commit()
        archive_note = ""
        if days > 7:
            archive_note = (
                " Note: Discord's visible thread auto-archive tier still caps at 7 days — "
                "archived threads auto-unarchive the moment `/submit` posts into them, so "
                "going beyond 7 only affects the submission cutoff, not the thread's displayed state."
            )
        await interaction.response.send_message(
            f"✅ Submission grace period set to **{days} day{'s' if days != 1 else ''}**."
            f"{clamp_note}{archive_note}",
            ephemeral=True,
        )

    @app_commands.command(
        name="set-catch-window",
        description="Set how many hours a submission stays catchable via /catch.",
    )
    @app_commands.describe(hours="Hours a submission stays catchable (1 to grace_period_days*24, default 24)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_catch_window(self, interaction: discord.Interaction, hours: int) -> None:
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, interaction.guild_id)
            if cfg is None:
                await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
                return
            if not _validate_catch_window(hours, cfg.grace_period_days):
                max_hours = cfg.grace_period_days * 24
                await interaction.response.send_message(
                    f"Catch window must be 1-{max_hours} hours (can't exceed the "
                    f"{cfg.grace_period_days}-day grace period).",
                    ephemeral=True,
                )
                return
            cfg.catch_window_hours = hours
            await s.commit()
        await interaction.response.send_message(
            f"✅ Catch window set to **{hours}h**.", ephemeral=True
        )

    @app_commands.command(name="pause", description="Pause daily posts.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def pause(self, interaction: discord.Interaction) -> None:
        await self._set_paused(interaction, True)

    @app_commands.command(name="resume", description="Resume daily posts.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def resume(self, interaction: discord.Interaction) -> None:
        await self._set_paused(interaction, False)

    async def _set_paused(self, interaction: discord.Interaction, paused: bool) -> None:
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, interaction.guild_id)
            if cfg is None:
                await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
                return
            cfg.paused = paused
            await s.commit()
        await interaction.response.send_message(
            "⏸️ Daily posts paused." if paused else "▶️ Daily posts resumed.", ephemeral=True
        )

    @app_commands.command(name="post-now", description="Post today's challenge immediately (admin/test).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def post_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, interaction.guild_id)
        if cfg is None or not cfg.channel_id:
            await interaction.followup.send("Run `/setup` first.", ephemeral=True)
            return
        local_date = datetime.now(ZoneInfo(cfg.timezone)).date()
        posted = await post_daily_for_guild(self.bot, self.bot.api, interaction.guild_id, local_date)
        await interaction.followup.send(
            "✅ Posted." if posted else "Already posted today (or paused / no channel).",
            ephemeral=True,
        )

    @app_commands.command(
        name="reset-pool",
        description="Manually reset the no-repeat selection pool for this server.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def reset_pool(self, interaction: discord.Interaction) -> None:
        view = ConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message(
            "⚠️ This will clear this server's no-repeat pool history — previously "
            "featured Pokémon may be picked again. This does **not** affect EXP, "
            "levels, or any player progress. Continue?",
            view=view,
            ephemeral=True,
        )
        view.interaction = interaction
        await view.wait()
        if not view.confirmed:
            return
        count = await clear_used_pool(interaction.guild_id)
        await interaction.edit_original_response(
            content=(
                f"✅ Cleared {count} Pokémon from the no-repeat pool. "
                "The cycle starts fresh from the next daily post."
            ),
            view=None,
        )

    # --- Weekly vote & wild encounters ---

    @app_commands.command(
        name="set-vote-day",
        description="Set the weekday the weekly wild-encounter category vote posts (day 2 is always the next day).",
    )
    @app_commands.describe(weekday="Weekday the category vote posts")
    @app_commands.choices(weekday=WEEKDAY_CHOICES)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_vote_day(self, interaction: discord.Interaction, weekday: app_commands.Choice[int]) -> None:
        gid = interaction.guild_id
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, gid)
            if cfg is None:
                await interaction.response.send_message("Run `/setup` first.", ephemeral=True)
                return
            cfg.vote_day1_weekday = weekday.value
            post_time, tz = cfg.post_time, cfg.timezone
            await s.commit()
        self.bot.reschedule_guild_votes(gid, weekday.value, post_time, tz)
        await interaction.response.send_message(
            f"✅ Weekly category vote now posts **{weekday.name}** (specific-choice vote the day after, "
            "resolution the day after that).",
            ephemeral=True,
        )

    @app_commands.command(name="event-create", description="Create an admin-authored wild-encounter Event.")
    @app_commands.describe(
        name="Event name, e.g. 'Eeveelution Week'",
        dex_numbers="Space or comma-separated dex numbers, e.g. '133 134 135 136 196 197 470 471'",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_create(self, interaction: discord.Interaction, name: str, dex_numbers: str) -> None:
        try:
            dex_list = _parse_dex_numbers(dex_numbers)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        async with db.session() as s:
            s.add(
                db.EventDefinition(
                    guild_id=interaction.guild_id,
                    name=name,
                    dex_list="\n".join(str(n) for n in dex_list),
                    created_by=interaction.user.id,
                )
            )
            await s.commit()
        await interaction.response.send_message(
            f"✅ Created event **{name}** with {len(dex_list)} Pokemon. "
            "It'll appear on the day-1 vote ballot from next week.",
            ephemeral=True,
        )

    @app_commands.command(name="event-list", description="List this server's wild-encounter Events.")
    async def event_list(self, interaction: discord.Interaction) -> None:
        async with db.session() as s:
            events = (
                await s.execute(
                    select(db.EventDefinition)
                    .where(db.EventDefinition.guild_id == interaction.guild_id)
                    .order_by(db.EventDefinition.is_active.desc(), db.EventDefinition.name)
                )
            ).scalars().all()
        if not events:
            await interaction.response.send_message(
                "No events yet — create one with `/event-create`.", ephemeral=True
            )
            return
        lines = [
            f"{'🟢' if e.is_active else '⚪'} **{e.name}** — {len(e.dex_numbers)} Pokemon"
            for e in events
        ]
        embed = discord.Embed(
            title="🎉 Wild-Encounter Events", description="\n".join(lines), color=0x5865F2
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _active_event_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        async with db.session() as s:
            events = (
                await s.execute(
                    select(db.EventDefinition.name).where(
                        db.EventDefinition.guild_id == interaction.guild_id, db.EventDefinition.is_active
                    )
                )
            ).scalars().all()
        cur = current.lower()
        return [
            app_commands.Choice(name=n, value=n) for n in events if cur in n.lower()
        ][:25]

    @app_commands.command(name="event-disable", description="Disable a wild-encounter Event without deleting it.")
    @app_commands.describe(name="Event name to disable")
    @app_commands.autocomplete(name=_active_event_name_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_disable(self, interaction: discord.Interaction, name: str) -> None:
        async with db.session() as s:
            event = (
                await s.execute(
                    select(db.EventDefinition).where(
                        db.EventDefinition.guild_id == interaction.guild_id,
                        db.EventDefinition.name == name,
                        db.EventDefinition.is_active,
                    )
                )
            ).scalar_one_or_none()
            if event is None:
                await interaction.response.send_message(
                    f"No active event named **{name}**.", ephemeral=True
                )
                return
            event.is_active = False
            await s.commit()
        await interaction.response.send_message(f"✅ Disabled event **{name}**.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
