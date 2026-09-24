"""PokeSketchDex bot: client, scheduler wiring, and extension loading."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord.ext import commands
from sqlalchemy import select

from . import db, gym, pokebox, weeklyvote
from .config import Config
from .daily import post_daily_for_guild, post_wild_encounter_for_guild
from .daily_spotlight import post_daily_spotlight_for_guild
from .default_events import seed_default_events
from .pokeapi import PokeApiClient

log = logging.getLogger(__name__)

INITIAL_EXTENSIONS = [
    "pokesketch.cogs.admin",
    "pokesketch.cogs.submissions",
    "pokesketch.cogs.collection",
    "pokesketch.cogs.stats",
    "pokesketch.cogs.profile",
    "pokesketch.cogs.gym",
    "pokesketch.cogs.help",
]

WEEKLY_POKEBALL_JOB_ID = "weekly-pokeball-grant"
GYM_EXPIRY_JOB_ID = "gym-expiry-check"
DAILY_SPOTLIGHT_JOB_ID = "daily-winner-spotlight"


class PokeSketchDexBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.members = True  # for role assignment / member lookups
        intents.message_content = False  # submissions use /submit, no privileged intent
        super().__init__(command_prefix="!ps ", intents=intents, help_command=None)
        self.config = config
        self.api = PokeApiClient(config.image_cache_dir)
        self.scheduler = AsyncIOScheduler()
        self._commands_synced = False

    async def setup_hook(self) -> None:
        db.init_engine(self.config.db_path)
        await db.create_all()
        for ext in INITIAL_EXTENSIONS:
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)

        # Slash commands are synced per-guild once we're actually connected
        # and self.guilds is populated — see on_ready / _sync_all_joined_guilds.
        # setup_hook runs before the gateway connects, so self.guilds is
        # empty here and any sync attempted now would be a no-op.

        await self._schedule_all_guilds()
        self._schedule_weekly_pokeball_grant()
        self._schedule_gym_expiry_check()
        self._schedule_daily_spotlight()
        self.scheduler.start()

    async def _sync_all_joined_guilds(self) -> None:
        """Sync slash commands guild-scoped only, to every guild we're in.

        We deliberately never register commands globally: mixing global +
        guild-scoped copies is what caused duplicate slash commands to show
        up client-side in a guild (both registrations render as separate
        entries). Copy the global (in-memory) command tree into each guild
        first, THEN clear the actual global registration on Discord's side —
        clearing first would leave nothing for copy_global_to() to copy.
        """
        for guild in self.guilds:
            target = discord.Object(id=guild.id)
            self.tree.copy_global_to(guild=target)
            await self.tree.sync(guild=target)
            log.info("Synced commands to guild %s (%s)", guild.id, guild.name)

        self.tree.clear_commands(guild=None)
        await self.tree.sync()  # pushes the (now empty) global command set
        log.info("Cleared any global command registrations.")

    async def _schedule_all_guilds(self) -> None:
        async with db.session() as s:
            cfgs = (await s.execute(select(db.GuildConfig))).scalars().all()
        for cfg in cfgs:
            self.reschedule_guild(cfg.guild_id, cfg.post_time, cfg.timezone)
            self.reschedule_guild_votes(cfg.guild_id, cfg.vote_day1_weekday, cfg.post_time, cfg.timezone)

    def reschedule_guild(self, guild_id: int, post_time: str, tz: str) -> None:
        """(Re)register the daily cron job for a guild."""
        job_id = f"daily-{guild_id}"
        existing = self.scheduler.get_job(job_id)
        if existing:
            existing.remove()
        try:
            hour, minute = (int(x) for x in post_time.split(":"))
            tzinfo = ZoneInfo(tz)
        except (ValueError, KeyError):
            log.warning("Guild %s: bad time/tz (%s / %s); skipping schedule.", guild_id, post_time, tz)
            return
        self.scheduler.add_job(
            self._run_daily_job,
            CronTrigger(hour=hour, minute=minute, timezone=tzinfo),
            id=job_id,
            args=[guild_id, tz],
            replace_existing=True,
            misfire_grace_time=3600,
        )
        log.info("Scheduled guild %s at %s %s", guild_id, post_time, tz)

    def unschedule_guild(self, guild_id: int) -> None:
        job = self.scheduler.get_job(f"daily-{guild_id}")
        if job:
            job.remove()

    async def _run_daily_job(self, guild_id: int, tz: str) -> None:
        local_date = datetime.now(ZoneInfo(tz)).date()
        try:
            await post_daily_for_guild(self, self.api, guild_id, local_date)
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler
            log.exception("Daily job failed for guild %s", guild_id)
        try:
            await post_wild_encounter_for_guild(self, self.api, guild_id, local_date)
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler
            log.exception("Wild encounter job failed for guild %s", guild_id)

    def reschedule_guild_votes(self, guild_id: int, weekday: int, post_time: str, tz: str) -> None:
        """(Re)register the day-1/day-2/resolution weekly-vote cron jobs for a
        guild. Day 2 always fires the day after day 1, and resolution the day
        after that — at the same time-of-day as the guild's daily post."""
        job_ids = (f"vote-day1-{guild_id}", f"vote-day2-{guild_id}", f"vote-resolve-{guild_id}")
        for job_id in job_ids:
            existing = self.scheduler.get_job(job_id)
            if existing:
                existing.remove()
        try:
            hour, minute = (int(x) for x in post_time.split(":"))
            tzinfo = ZoneInfo(tz)
        except (ValueError, KeyError):
            log.warning("Guild %s: bad time/tz (%s / %s); skipping vote schedule.", guild_id, post_time, tz)
            return
        if not 0 <= weekday <= 6:
            log.warning("Guild %s: bad vote_day1_weekday %s; skipping vote schedule.", guild_id, weekday)
            return
        day1_id, day2_id, resolve_id = job_ids
        day2_weekday = (weekday + 1) % 7
        resolve_weekday = (weekday + 2) % 7
        self.scheduler.add_job(
            self._run_vote_day1_job,
            CronTrigger(day_of_week=weekday, hour=hour, minute=minute, timezone=tzinfo),
            id=day1_id, args=[guild_id], replace_existing=True, misfire_grace_time=3600,
        )
        self.scheduler.add_job(
            self._run_vote_day2_job,
            CronTrigger(day_of_week=day2_weekday, hour=hour, minute=minute, timezone=tzinfo),
            id=day2_id, args=[guild_id], replace_existing=True, misfire_grace_time=3600,
        )
        self.scheduler.add_job(
            self._run_vote_resolve_job,
            CronTrigger(day_of_week=resolve_weekday, hour=hour, minute=minute, timezone=tzinfo),
            id=resolve_id, args=[guild_id], replace_existing=True, misfire_grace_time=3600,
        )
        log.info(
            "Scheduled guild %s vote cycle: day1=%s day2=%s resolve=%s at %s %s",
            guild_id, weekday, day2_weekday, resolve_weekday, post_time, tz,
        )

    def unschedule_guild_votes(self, guild_id: int) -> None:
        for job_id in (f"vote-day1-{guild_id}", f"vote-day2-{guild_id}", f"vote-resolve-{guild_id}"):
            job = self.scheduler.get_job(job_id)
            if job:
                job.remove()

    async def _run_vote_day1_job(self, guild_id: int) -> None:
        try:
            await weeklyvote.post_category_poll(self, guild_id)
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler
            log.exception("Vote day-1 job failed for guild %s", guild_id)

    async def _run_vote_day2_job(self, guild_id: int) -> None:
        try:
            await weeklyvote.resolve_category_poll(self, self.api, guild_id)
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler
            log.exception("Vote day-2 job failed for guild %s", guild_id)

    async def _run_vote_resolve_job(self, guild_id: int) -> None:
        try:
            await weeklyvote.resolve_choice_poll(self, self.api, guild_id)
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler
            log.exception("Vote resolve job failed for guild %s", guild_id)

    def _schedule_weekly_pokeball_grant(self) -> None:
        """Global (not per-guild) weekly pokeball grant — runs once regardless of guild count."""
        self.scheduler.add_job(
            self._run_weekly_pokeball_job,
            CronTrigger(day_of_week="mon", hour=0, minute=0, timezone=ZoneInfo("UTC")),
            id=WEEKLY_POKEBALL_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        log.info("Scheduled weekly pokeball grant (Monday 00:00 UTC).")

    async def _run_weekly_pokeball_job(self) -> None:
        try:
            async with db.session() as s:
                granted = await pokebox.grant_weekly_pokeballs(s)
                await s.commit()
            log.info("Weekly pokeball grant: %d users granted.", granted)
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler
            log.exception("Weekly pokeball grant job failed")

    def _schedule_gym_expiry_check(self) -> None:
        """Global (not per-guild) daily tick that closes any guild's active
        GymEvent whose ends_at has passed — daily granularity is enough per
        the design doc's locked timeout behavior (no badge on expiry)."""
        self.scheduler.add_job(
            self._run_gym_expiry_job,
            CronTrigger(hour=0, minute=10, timezone=ZoneInfo("UTC")),
            id=GYM_EXPIRY_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        log.info("Scheduled gym expiry check (daily 00:10 UTC).")

    async def _run_gym_expiry_job(self) -> None:
        try:
            async with db.session() as s:
                expired = await gym.close_expired_gym_events(s)
                await s.commit()
            if expired:
                log.info("Gym expiry check: %d event(s) expired.", len(expired))
        except Exception:  # noqa: BLE001 - never let a job kill the scheduler
            log.exception("Gym expiry check job failed")

    def _schedule_daily_spotlight(self) -> None:
        """Global (not per-guild) daily tick that posts each guild's previous-
        day top-upvoted submission(s) (computed in that guild's own local
        timezone) and awards EXP_DAILY_WINNER. A few minutes before the
        gym-expiry check, at a fixed global UTC time rather than per-guild
        post_time — per-guild-timezone scheduling would need a separate job
        per guild's own morning, which is more complexity than v1 needs; see
        the Daily Winner Spotlight design doc for why a single global tick
        (with "yesterday" computed per guild inside the job body) is the
        accepted v1 simplification."""
        self.scheduler.add_job(
            self._run_daily_spotlight_job,
            CronTrigger(hour=0, minute=5, timezone=ZoneInfo("UTC")),
            id=DAILY_SPOTLIGHT_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        log.info("Scheduled daily winner spotlight (daily 00:05 UTC).")

    async def _run_daily_spotlight_job(self) -> None:
        async with db.session() as s:
            cfgs = (await s.execute(select(db.GuildConfig))).scalars().all()
        for cfg in cfgs:
            try:
                tzinfo = ZoneInfo(cfg.timezone)
            except KeyError:
                log.warning("Guild %s: bad timezone %s; skipping spotlight.", cfg.guild_id, cfg.timezone)
                continue
            local_date = datetime.now(tzinfo).date() - timedelta(days=1)
            try:
                await post_daily_spotlight_for_guild(self, cfg.guild_id, local_date)
            except Exception:  # noqa: BLE001 - never let a job kill the scheduler
                log.exception("Daily spotlight job failed for guild %s", cfg.guild_id)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        if not self._commands_synced:
            await self._sync_all_joined_guilds()
            self._commands_synced = True

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Sync commands to a newly-joined guild immediately, and seed its
        built-in wild-encounter Events so they're ready even before /setup
        runs (seeding is idempotent, so /setup re-seeding is harmless too —
        see cogs/admin.py's setup_cmd, which covers guilds that joined before
        this feature shipped)."""
        target = discord.Object(id=guild.id)
        self.tree.copy_global_to(guild=target)
        await self.tree.sync(guild=target)
        log.info("Synced commands to newly joined guild %s (%s)", guild.id, guild.name)
        async with db.session() as s:
            seeded = await seed_default_events(s, self.api, guild.id)
            await s.commit()
        if seeded:
            log.info("Guild %s: seeded %d built-in events on join.", guild.id, len(seeded))

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.api.aclose()
        await db.dispose()
        await super().close()
