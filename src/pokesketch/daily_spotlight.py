"""Daily Winner Spotlight: announce + award EXP for the previous day's
top-upvoted submission(s). Mirrors daily.py's job-module shape: a plain async
function per guild, callable from the scheduler AND from a test, not a cog
method.
"""

from __future__ import annotations

import logging
from datetime import date

import discord
from sqlalchemy import func, select

from . import db, leveling
from .cogs.submissions import _award_exp
from .formatting import species_display_name

log = logging.getLogger(__name__)

EXP_EVENT_TYPE = "daily_winner"


async def _already_spotlighted_today(s, guild_id: int) -> bool:
    """Idempotency guard against a restart double-firing the same run.

    Same "already did X today" pattern as the upvote-EXP daily caps in
    cogs/submissions.py (func.date(created_at) == func.date(now)) rather than
    a new GuildStats column. This job is a single global daily cron tick, so
    "already ran today" and "already spotlighted yesterday's winner" are
    equivalent for the scheduled path (see bot.py's daily-spotlight job).
    """
    existing = (
        await s.execute(
            select(db.ExpEvent.id).where(
                db.ExpEvent.guild_id == guild_id,
                db.ExpEvent.type == EXP_EVENT_TYPE,
                func.date(db.ExpEvent.created_at) == func.date(func.now()),
            )
        )
    ).scalar_one_or_none()
    return existing is not None


async def _find_winners(
    s, guild_id: int, local_date: date
) -> tuple[db.DailyPokemon | None, list[db.Submission], int]:
    """For a guild+date, return (daily, winning submissions, top upvote
    count). Winning submissions is empty if that day had no submissions."""
    daily = (
        await s.execute(
            select(db.DailyPokemon).where(
                db.DailyPokemon.guild_id == guild_id,
                db.DailyPokemon.local_date == local_date,
            )
        )
    ).scalar_one_or_none()
    if daily is None:
        return None, [], 0

    upvote_count = func.count(db.Upvote.id).label("upvote_count")
    rows = (
        await s.execute(
            select(db.Submission, upvote_count)
            .outerjoin(db.Upvote, db.Upvote.submission_id == db.Submission.id)
            .where(db.Submission.daily_id == daily.id)
            .group_by(db.Submission.id)
        )
    ).all()
    if not rows:
        return daily, [], 0

    top_count = max(count for _sub, count in rows)
    winners = [sub for sub, count in rows if count == top_count]
    return daily, winners, top_count


def _spotlight_embed(
    local_date: date, dex_no: int, species: str, shiny: bool, upvote_count: int, winners: list[db.Submission]
) -> discord.Embed:
    shiny_tag = " ✨(Shiny!)" if shiny else ""
    mentions = ", ".join(f"<@{sub.user_id}>" for sub in winners)
    upvote_label = "upvote" if upvote_count == 1 else "upvotes"
    if len(winners) > 1:
        title = f"🏆 Yesterday's Top Sketches: #{dex_no:04d} {species}{shiny_tag}"
        lead = f"{mentions} tied for the top spot with {upvote_count} {upvote_label} each!"
    else:
        title = f"🏆 Yesterday's Top Sketch: #{dex_no:04d} {species}{shiny_tag}"
        lead = f"{mentions}'s sketch won the day with {upvote_count} {upvote_label}!"
    embed = discord.Embed(
        title=title,
        description=f"{lead}\n\n+{leveling.EXP_DAILY_WINNER} EXP awarded!",
        color=0xFFD700,
    )
    if winners[0].image_url:
        embed.set_image(url=winners[0].image_url)
    embed.set_footer(text=f"PokeSketchDex • Daily Winner Spotlight • {local_date.isoformat()}")
    return embed


async def post_daily_spotlight_for_guild(client: discord.Client, guild_id: int, local_date: date) -> bool:
    """Post the spotlight for one guild's `local_date` and award EXP to the
    winning artist(s). `local_date` is the day being spotlighted (typically
    "yesterday" relative to when the scheduler fires), not "today". Returns
    True if a spotlight was posted."""
    async with db.session() as s:
        cfg = await s.get(db.GuildConfig, guild_id)
        if cfg is None or cfg.paused or not cfg.channel_id:
            return False
        if await _already_spotlighted_today(s, guild_id):
            log.info("Guild %s: spotlight already ran today; skipping.", guild_id)
            return False
        daily, winners, top_count = await _find_winners(s, guild_id, local_date)
        if daily is None or not winners:
            return False
        channel_id = cfg.channel_id
        winner_user_ids = [sub.user_id for sub in winners]
        species = species_display_name(daily.name)
        shiny, dex_no = daily.is_shiny, daily.dex_no
        embed = _spotlight_embed(local_date, dex_no, species, shiny, top_count, winners)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException:
            log.warning("Guild %s: channel %s not found.", guild_id, channel_id)
            return False

    await channel.send(embed=embed)

    async with db.session() as s:
        for user_id in winner_user_ids:
            await _award_exp(s, guild_id, user_id, EXP_EVENT_TYPE, leveling.EXP_DAILY_WINNER)
        await s.commit()

    log.info(
        "Guild %s: posted daily spotlight for %s (%d winner(s), %d upvotes each, +%d EXP).",
        guild_id, local_date, len(winner_user_ids), top_count, leveling.EXP_DAILY_WINNER,
    )
    return True
