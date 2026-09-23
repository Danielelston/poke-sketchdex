"""Daily-post orchestration: choose, fetch, post announcement + thread."""

from __future__ import annotations

import logging
import random
from datetime import date

import discord
from sqlalchemy import select

from . import db, weeklyvote
from .embeds import daily_embed
from .pokeapi import PokeApiClient
from .selection import pick_dex_no

log = logging.getLogger(__name__)

SHINY_CHANCE = 1 / 40  # ~2.5% chance the daily reference is shiny

# Discord's actual supported auto-archive tiers, in minutes (1h/1d/3d/1wk) — no
# arbitrary durations are allowed, so grace_period_days must snap to one of these.
ARCHIVE_DURATION_TIERS = (60, 1440, 4320, 10080)


def _archive_duration_for_grace(grace_period_days: int) -> int:
    """Smallest Discord archive tier >= grace_period_days * 1440 minutes, capped
    at the largest tier (10080 = 1 week). Default 7 days -> 10080 exactly, so
    nothing changes visually for guilds left at the default grace period."""
    target = grace_period_days * 1440
    for tier in ARCHIVE_DURATION_TIERS:
        if tier >= target:
            return tier
    return ARCHIVE_DURATION_TIERS[-1]


async def post_daily_for_guild(
    client: discord.Client,
    api: PokeApiClient,
    guild_id: int,
    local_date: date,
) -> bool:
    """Post the daily challenge for one guild. Returns True if posted."""
    async with db.session() as s:
        cfg = await s.get(db.GuildConfig, guild_id)
        if cfg is None or cfg.paused or not cfg.channel_id:
            return False
        # Idempotency: don't double-post the same local date.
        existing = (
            await s.execute(
                select(db.DailyPokemon).where(
                    db.DailyPokemon.guild_id == guild_id,
                    db.DailyPokemon.local_date == local_date,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.info("Guild %s already has a daily for %s; skipping.", guild_id, local_date)
            return False
        channel_id, role_id = cfg.channel_id, cfg.role_id
        dex_min, dex_max, mode = cfg.dex_min, cfg.dex_max, cfg.selection_mode
        grace_period_days = cfg.grace_period_days

    dex_no = await pick_dex_no(guild_id, dex_min, dex_max, mode)
    ref = await api.get_pokemon(dex_no)
    shiny = random.random() < SHINY_CHANCE
    images = ref.reference_images(shiny=shiny)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException:
            log.warning("Guild %s: channel %s not found.", guild_id, channel_id)
            return False

    role_mention = f"<@&{role_id}>" if role_id else ""
    embed = daily_embed(ref, shiny, images)
    msg = await channel.send(
        content=f"{role_mention} 🎨 A new Pokemon to sketch today!".strip(),
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True),
    )

    thread = None
    try:
        thread = await msg.create_thread(
            name=f"{local_date.isoformat()} — {ref.display_name()}",
            auto_archive_duration=_archive_duration_for_grace(grace_period_days),
        )
        await thread.send(
            "Post your sketches here with `/submit`! You can also chat about "
            "today's Pokemon. React 👍 to upvote entries."
        )
    except discord.DiscordException as exc:
        log.warning("Guild %s: could not create thread: %s", guild_id, exc)

    async with db.session() as s:
        s.add(
            db.DailyPokemon(
                guild_id=guild_id,
                local_date=local_date,
                dex_no=ref.dex_no,
                name=ref.name,
                is_shiny=shiny,
                ref_image_urls="\n".join(images),
                announce_message_id=msg.id,
                thread_id=thread.id if thread else None,
            )
        )
        await s.commit()
    log.info("Guild %s: posted daily #%s %s (shiny=%s).", guild_id, ref.dex_no, ref.name, shiny)
    return True


async def post_wild_encounter_for_guild(
    client: discord.Client,
    api: PokeApiClient,
    guild_id: int,
    local_date: date,
) -> bool:
    """Post today's fresh random pick into the guild's active wild-encounter
    thread, if it has one. "Active" is the most recent WeeklyVote with a
    non-null thread_id (see weeklyvote.get_active_weekly_vote) so the
    previous cycle's thread keeps getting daily posts with no gap while this
    week's day-1/day-2 votes are still in progress.
    """
    async with db.session() as s:
        wv = await weeklyvote.get_active_weekly_vote(s, guild_id)
        if wv is None or wv.thread_id is None:
            return False
        dex_pool = wv.dex_pool_numbers
        if not dex_pool:
            return False
        existing = (
            await s.execute(
                select(db.WildEncounter).where(
                    db.WildEncounter.guild_id == guild_id, db.WildEncounter.local_date == local_date
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False  # idempotent: already posted today
        thread_id, weekly_vote_id = wv.thread_id, wv.id

    dex_no = random.choice(dex_pool)
    ref = await api.get_pokemon(dex_no)
    images = ref.reference_images(shiny=False)

    thread = client.get_channel(thread_id)
    if thread is None:
        try:
            thread = await client.fetch_channel(thread_id)
        except discord.DiscordException:
            log.warning("Guild %s: wild-encounter thread %s not found.", guild_id, thread_id)
            return False

    embed = daily_embed(ref, False, images)
    embed.title = f"🌿 Wild encounter: #{ref.dex_no:04d} {ref.display_name()}"
    msg = await thread.send(
        content="A wild Pokemon appeared! Sketch it with `/submit`.", embed=embed
    )

    async with db.session() as s:
        s.add(
            db.WildEncounter(
                guild_id=guild_id,
                weekly_vote_id=weekly_vote_id,
                local_date=local_date,
                dex_no=ref.dex_no,
                name=ref.name,
                message_id=msg.id,
            )
        )
        await s.commit()
    log.info("Guild %s: posted wild encounter #%s %s.", guild_id, ref.dex_no, ref.name)
    return True
