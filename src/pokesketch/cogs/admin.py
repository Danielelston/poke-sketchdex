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
from ..default_events import MAX_EVENTS_PER_GUILD, seed_default_events
from ..discord_limits import DISCORD_EMBED_FIELD_VALUE_LIMIT, guarded_add_field
from ..selection import clear_used_pool
from ..ui import ConfirmView, PaginatorView

log = logging.getLogger(__name__)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_ALL_TZ = available_timezones()

GRACE_PERIOD_MIN_DAYS = 1
GRACE_PERIOD_MAX_DAYS = 30

MIN_DEX_NO = 1
MAX_DEX_NO = 1025

EVENT_NAME_MAX_LEN = 80
EVENT_FLAVOR_TEXT_MAX_LEN = 300
EVENT_MAX_DEX_NUMBERS = 200
EVENT_LIST_PAGE_SIZE = 15
# A resolved event pool serves all 7 days of the week via random.choice with
# no repeat-avoidance, so anything under this guarantees repeats in a week.
EVENT_MIN_RECOMMENDED_DEX = 7

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
    if len(seen) > EVENT_MAX_DEX_NUMBERS:
        raise ValueError(
            f"That's {len(seen)} distinct dex numbers — events are capped at {EVENT_MAX_DEX_NUMBERS}."
        )
    return seen


def _render_dex_numbers(dex_numbers: list[int]) -> str:
    """Render dex numbers as a comma-separated string, safely truncated to fit
    under Discord's embed field value limit, e.g. "#1, #2, … (+198 more)"."""
    tokens = [f"#{n}" for n in dex_numbers]
    full = ", ".join(tokens)
    if len(full) <= DISCORD_EMBED_FIELD_VALUE_LIMIT:
        return full
    kept = len(tokens)
    while kept > 0:
        kept -= 1
        truncated = ", ".join(tokens[:kept])
        suffix = f"… (+{len(tokens) - kept} more)"
        candidate = f"{truncated}, {suffix}" if truncated else suffix
        if len(candidate) <= DISCORD_EMBED_FIELD_VALUE_LIMIT:
            return candidate
    return f"… (+{len(tokens)} more)"


def _short_event_warning(dex_count: int) -> str | None:
    """Non-blocking warning for /event-create and /event-edit when a dex list
    is under EVENT_MIN_RECOMMENDED_DEX — None if there's nothing to warn about."""
    if dex_count >= EVENT_MIN_RECOMMENDED_DEX:
        return None
    return (
        f"⚠️ Only {dex_count} Pokemon — expect repeats within the week (a resolved event pool "
        "serves all 7 days). Add more for full variety if you'd like."
    )


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
            await s.commit()
            if await s.get(db.GuildStats, gid) is None:
                s.add(db.GuildStats(guild_id=gid))
                await s.commit()
        async with db.session() as s:
            # Idempotent — no-ops for guilds already seeded (e.g. on join, or a
            # previous /setup run). Covers guilds that joined before this
            # feature shipped, since they'll never fire on_guild_join again.
            await seed_default_events(s, self.bot.api, gid)
            await s.commit()
        self.bot.reschedule_guild(gid, time, timezone)
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
    @app_commands.describe(
        dex_no=(
            "Optional: force this exact dex number instead of the normal random pick — e.g. to test "
            "the alt-forms picker against a known multi-form species (try 774 Minior or 413 Wormadam). "
            "Replaces today's post if one already exists, so you can re-run this multiple times."
        )
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def post_now(
        self, interaction: discord.Interaction, dex_no: app_commands.Range[int, MIN_DEX_NO, MAX_DEX_NO] | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, interaction.guild_id)
        if cfg is None or not cfg.channel_id:
            await interaction.followup.send("Run `/setup` first.", ephemeral=True)
            return
        local_date = datetime.now(ZoneInfo(cfg.timezone)).date()
        posted = await post_daily_for_guild(
            self.bot, self.bot.api, interaction.guild_id, local_date, dex_no_override=dex_no
        )
        if posted:
            msg = f"✅ Posted with forced dex #{dex_no}." if dex_no is not None else "✅ Posted."
        else:
            msg = "Already posted today (or paused / no channel)."
        await interaction.followup.send(msg, ephemeral=True)

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
            await s.commit()
        await interaction.response.send_message(
            f"✅ Weekly category vote now posts **{weekday.name}** (specific-choice vote the day after, "
            "resolution the day after that).",
            ephemeral=True,
        )

    @app_commands.command(name="event-create", description="Create an admin-authored wild-encounter Event.")
    @app_commands.describe(
        name="Event name, e.g. 'Eeveelution Week'",
        dex_numbers="Space or comma-separated dex numbers, e.g. '133 134 135 136 196 197 470 471' "
        f"(use at least {EVENT_MIN_RECOMMENDED_DEX} for a full week without repeats)",
        flavor_text="Optional short note on why this event exists, e.g. "
        "'Spooky-themed pool for the Halloween season'",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_create(
        self,
        interaction: discord.Interaction,
        name: str,
        dex_numbers: str,
        flavor_text: str | None = None,
    ) -> None:
        if len(name) > EVENT_NAME_MAX_LEN:
            await interaction.response.send_message(
                f"❌ Event name is {len(name)} chars — keep it under {EVENT_NAME_MAX_LEN}.",
                ephemeral=True,
            )
            return
        if flavor_text is not None and len(flavor_text) > EVENT_FLAVOR_TEXT_MAX_LEN:
            await interaction.response.send_message(
                f"❌ Flavor text is {len(flavor_text)} chars — keep it under {EVENT_FLAVOR_TEXT_MAX_LEN}.",
                ephemeral=True,
            )
            return
        try:
            dex_list = _parse_dex_numbers(dex_numbers)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        async with db.session() as s:
            existing = (
                await s.execute(
                    select(db.EventDefinition).where(db.EventDefinition.guild_id == interaction.guild_id)
                )
            ).scalars().all()
            if len(existing) >= MAX_EVENTS_PER_GUILD:
                await interaction.response.send_message(
                    f"❌ This server already has {len(existing)}/{MAX_EVENTS_PER_GUILD} events — that's "
                    "the cap. Disable an unused one with `/event-disable` to free up room.",
                    ephemeral=True,
                )
                return
            if any(e.name.lower() == name.lower() for e in existing):
                await interaction.response.send_message(
                    f"❌ An event named **{name}** already exists (active or disabled) — "
                    "pick a different name.",
                    ephemeral=True,
                )
                return
            s.add(
                db.EventDefinition(
                    guild_id=interaction.guild_id,
                    name=name,
                    flavor_text=flavor_text,
                    dex_list="\n".join(str(n) for n in dex_list),
                    created_by=interaction.user.id,
                )
            )
            await s.commit()
        msg = (
            f"✅ Created event **{name}** with {len(dex_list)} Pokemon. "
            "It'll appear on the day-1 vote ballot from next week."
        )
        warning = _short_event_warning(len(dex_list))
        if warning:
            msg += f"\n{warning}"
        await interaction.response.send_message(msg, ephemeral=True)

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
        chunks = [lines[i : i + EVENT_LIST_PAGE_SIZE] for i in range(0, len(lines), EVENT_LIST_PAGE_SIZE)]
        pages = []
        for page_no, chunk in enumerate(chunks, start=1):
            embed = discord.Embed(
                title="🎉 Wild-Encounter Events", description="\n".join(chunk), color=0x5865F2
            )
            embed.set_footer(text=f"Page {page_no}/{len(chunks)} • {len(events)} events total")
            pages.append(embed)

        if len(pages) == 1:
            await interaction.response.send_message(embed=pages[0], ephemeral=True)
            return
        view = PaginatorView(pages, author_id=interaction.user.id)
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)
        view.message = await interaction.original_response()

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

    async def _disabled_event_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        async with db.session() as s:
            events = (
                await s.execute(
                    select(db.EventDefinition.name).where(
                        db.EventDefinition.guild_id == interaction.guild_id,
                        db.EventDefinition.is_active.is_(False),
                    )
                )
            ).scalars().all()
        cur = current.lower()
        return [
            app_commands.Choice(name=n, value=n) for n in events if cur in n.lower()
        ][:25]

    async def _all_event_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        async with db.session() as s:
            events = (
                await s.execute(
                    select(db.EventDefinition.name).where(
                        db.EventDefinition.guild_id == interaction.guild_id
                    )
                )
            ).scalars().all()
        cur = current.lower()
        return [
            app_commands.Choice(name=n, value=n) for n in events if cur in n.lower()
        ][:25]

    @app_commands.command(name="event-view", description="View a wild-encounter Event's status and contents.")
    @app_commands.describe(name="Event name to view")
    @app_commands.autocomplete(name=_all_event_name_autocomplete)
    async def event_view(self, interaction: discord.Interaction, name: str) -> None:
        async with db.session() as s:
            event = (
                await s.execute(
                    select(db.EventDefinition).where(
                        db.EventDefinition.guild_id == interaction.guild_id,
                        db.EventDefinition.name == name,
                    )
                )
            ).scalars().first()
        if event is None:
            await interaction.response.send_message(f"No event named **{name}**.", ephemeral=True)
            return
        embed = discord.Embed(title=event.name, color=0x5865F2)
        guarded_add_field(embed, "Status", "🟢 Active" if event.is_active else "⚪ Disabled", inline=True)
        guarded_add_field(embed, "Pokemon count", f"{len(event.dex_numbers)} Pokemon", inline=True)
        guarded_add_field(
            embed,
            "Flavor text",
            event.flavor_text or "_No flavor text set — add one with `/event-edit`._",
        )
        guarded_add_field(embed, "Dex numbers", _render_dex_numbers(event.dex_numbers))
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

    @app_commands.command(name="event-enable", description="Re-enable a disabled wild-encounter Event.")
    @app_commands.describe(name="Disabled event name to re-enable")
    @app_commands.autocomplete(name=_disabled_event_name_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_enable(self, interaction: discord.Interaction, name: str) -> None:
        async with db.session() as s:
            event = (
                await s.execute(
                    select(db.EventDefinition).where(
                        db.EventDefinition.guild_id == interaction.guild_id,
                        db.EventDefinition.name == name,
                        db.EventDefinition.is_active.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if event is None:
                await interaction.response.send_message(
                    f"No disabled event named **{name}**.", ephemeral=True
                )
                return
            event.is_active = True
            await s.commit()
        await interaction.response.send_message(f"✅ Re-enabled event **{name}**.", ephemeral=True)

    @app_commands.command(name="event-edit", description="Edit an existing wild-encounter Event.")
    @app_commands.describe(
        name="Event to edit",
        new_name="New name for the event",
        dex_numbers="Replace the dex number list (space/comma-separated) "
        f"— use at least {EVENT_MIN_RECOMMENDED_DEX} for a full week without repeats",
        flavor_text="Replace the flavor text",
    )
    @app_commands.autocomplete(name=_all_event_name_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def event_edit(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str | None = None,
        dex_numbers: str | None = None,
        flavor_text: str | None = None,
    ) -> None:
        if new_name is None and dex_numbers is None and flavor_text is None:
            await interaction.response.send_message(
                "❌ Provide at least one of `new_name`, `dex_numbers`, or `flavor_text` to change.",
                ephemeral=True,
            )
            return
        if new_name is not None and len(new_name) > EVENT_NAME_MAX_LEN:
            await interaction.response.send_message(
                f"❌ Event name is {len(new_name)} chars — keep it under {EVENT_NAME_MAX_LEN}.",
                ephemeral=True,
            )
            return
        if flavor_text is not None and len(flavor_text) > EVENT_FLAVOR_TEXT_MAX_LEN:
            await interaction.response.send_message(
                f"❌ Flavor text is {len(flavor_text)} chars — keep it under {EVENT_FLAVOR_TEXT_MAX_LEN}.",
                ephemeral=True,
            )
            return
        new_dex_list: list[int] | None = None
        if dex_numbers is not None:
            try:
                new_dex_list = _parse_dex_numbers(dex_numbers)
            except ValueError as exc:
                await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
                return

        async with db.session() as s:
            event = (
                await s.execute(
                    select(db.EventDefinition).where(
                        db.EventDefinition.guild_id == interaction.guild_id,
                        db.EventDefinition.name == name,
                    )
                )
            ).scalars().first()
            if event is None:
                await interaction.response.send_message(f"❌ No event named **{name}**.", ephemeral=True)
                return

            if new_name is not None and new_name.lower() != event.name.lower():
                others = (
                    await s.execute(
                        select(db.EventDefinition).where(
                            db.EventDefinition.guild_id == interaction.guild_id,
                            db.EventDefinition.id != event.id,
                        )
                    )
                ).scalars().all()
                if any(e.name.lower() == new_name.lower() for e in others):
                    await interaction.response.send_message(
                        f"❌ An event named **{new_name}** already exists (active or disabled) — "
                        "pick a different name.",
                        ephemeral=True,
                    )
                    return

            old_name = event.name
            changes = []
            if new_name is not None and new_name != event.name:
                event.name = new_name
                changes.append(f"renamed to **{new_name}**")
            if new_dex_list is not None:
                event.dex_list = "\n".join(str(n) for n in new_dex_list)
                changes.append(f"dex list now {len(new_dex_list)} Pokemon")
            if flavor_text is not None:
                event.flavor_text = flavor_text
                changes.append("flavor text updated")
            await s.commit()

        if not changes:
            await interaction.response.send_message(
                f"Nothing changed for **{old_name}** — provided values matched the current settings.",
                ephemeral=True,
            )
            return
        msg = f"✅ Updated **{old_name}**: {', '.join(changes)}."
        if new_dex_list is not None:
            warning = _short_event_warning(len(new_dex_list))
            if warning:
                msg += f"\n{warning}"
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
