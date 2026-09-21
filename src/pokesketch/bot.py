"""PokeSketch bot: client, scheduler wiring, and extension loading."""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord.ext import commands
from sqlalchemy import select

from . import db
from .config import Config
from .daily import post_daily_for_guild
from .pokeapi import PokeApiClient

log = logging.getLogger(__name__)

INITIAL_EXTENSIONS = [
    "pokesketch.cogs.admin",
    "pokesketch.cogs.submissions",
    "pokesketch.cogs.stats",
]


class PokeSketchBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        intents.members = True  # for role assignment / member lookups
        intents.message_content = False  # submissions use /submit, no privileged intent
        super().__init__(command_prefix="!ps ", intents=intents, help_command=None)
        self.config = config
        self.api = PokeApiClient(config.image_cache_dir)
        self.scheduler = AsyncIOScheduler()

    async def setup_hook(self) -> None:
        db.init_engine(self.config.db_path)
        await db.create_all()
        for ext in INITIAL_EXTENSIONS:
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)

        # Sync slash commands: instantly to dev guilds, else globally.
        if self.config.dev_guild_ids:
            for gid in self.config.dev_guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Synced commands to dev guild %s", gid)
        else:
            await self.tree.sync()
            log.info("Synced global commands (may take up to ~1h to appear).")

        await self._schedule_all_guilds()
        self.scheduler.start()

    async def _schedule_all_guilds(self) -> None:
        async with db.session() as s:
            cfgs = (await s.execute(select(db.GuildConfig))).scalars().all()
        for cfg in cfgs:
            self.reschedule_guild(cfg.guild_id, cfg.post_time, cfg.timezone)

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

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.api.aclose()
        await db.dispose()
        await super().close()
