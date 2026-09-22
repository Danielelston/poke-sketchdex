"""PokeSketchDex bot: client, scheduler wiring, and extension loading."""

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
    "pokesketch.cogs.help",
]


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
        if not self._commands_synced:
            await self._sync_all_joined_guilds()
            self._commands_synced = True

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Sync commands to a newly-joined guild immediately."""
        target = discord.Object(id=guild.id)
        self.tree.copy_global_to(guild=target)
        await self.tree.sync(guild=target)
        log.info("Synced commands to newly joined guild %s (%s)", guild.id, guild.name)

    async def close(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        await self.api.aclose()
        await db.dispose()
        await super().close()
