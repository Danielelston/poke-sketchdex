"""Daily-post orchestration: choose, fetch, post announcement + thread."""

from __future__ import annotations

import logging
import random
from datetime import date

import discord
from sqlalchemy import select

from . import db
from .embeds import daily_embed
from .pokeapi import PokeApiClient
from .selection import pick_dex_no

log = logging.getLogger(__name__)

SHINY_CHANCE = 1 / 40  # ~2.5% chance the daily reference is shiny


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
            auto_archive_duration=1440,
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
