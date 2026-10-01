"""Daily-post orchestration: choose, fetch, post announcement + thread."""

from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import discord
from sqlalchemy import select

from . import db, pokebox, weeklyvote
from .embeds import daily_embed
from .pokeapi import PokeApiClient
from .selection import pick_dex_no

log = logging.getLogger(__name__)

SHINY_CHANCE = 1 / 40  # ~2.5% chance the daily reference is shiny

# Fixed duration /submit stays open on a wild-encounter thread, counted from
# that thread's own post time (NOT a guild-local-midnight cutoff like the
# main daily's grace period) — see _wild_encounter_window_summary_line and
# _find_wild_encounter_for_thread in submissions.py.
WILD_ENCOUNTER_SUBMIT_WINDOW_HOURS = 24

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


def _submit_deadline_utc(local_date: date, grace_period_days: int, tz_name: str) -> datetime:
    """First guild-local midnight at/after which /submit for this thread's
    daily is rejected (see _is_outside_grace_window in submissions.py — it's
    a pure date comparison, so the cutoff is always a guild-local midnight,
    never a sub-day time). Returned as naive-UTC to match this project's
    DB-datetime convention (see pokebox._naive_utcnow)."""
    tz = ZoneInfo(tz_name)
    cutoff_local_date = local_date + timedelta(days=grace_period_days + 1)
    cutoff_local_midnight = datetime.combine(cutoff_local_date, time.min, tzinfo=tz)
    return cutoff_local_midnight.astimezone(UTC).replace(tzinfo=None)


def _window_summary_line(local_date: date, grace_period_days: int, catch_window_hours: int, tz_name: str) -> str:
    """Live, self-updating summary of the two windows for a daily thread's
    opening post, using Discord's <t:UNIX:R> markdown (renders client-side and
    keeps ticking with no bot-side re-editing needed — see the Obsidian design
    doc for why a static ASCII bar was rejected: it can't reflect elapsed time).
    The submit deadline is a concrete guild-local-midnight cutoff for this
    specific thread. The catch window is stated as "closes Nh/Nd after it's
    submitted" (a per-submission duration, not a shared deadline) because it
    resets fresh at each individual /submit's own created_at — NOT at the
    thread's post time — so a late submitter (right up to the submit deadline)
    still gets their own full catch window, which correctly extends past the
    submit deadline shown here (see catch_label_parts/pokebox.catchable_submissions
    and the "late submitter" smoke test in scripts/smoke_test.py)."""
    submit_deadline = pokebox.discord_timestamp(
        _submit_deadline_utc(local_date, grace_period_days, tz_name), style="R"
    )
    catch_label = pokebox.format_duration_label(catch_window_hours)
    return (
        f"⏳ `/submit` closes for this thread {submit_deadline} • "
        f"`/catch` closes {catch_label} after each sketch is submitted."
    )


async def _forms_view_for_dex(client: discord.Client, api: PokeApiClient, dex_no: int) -> discord.ui.View | None:
    """Resolve the persistent "View Alt Forms" view to attach to a fresh
    daily/wild-encounter announcement, or None when the species has only one
    eligible natural form (today's single-embed post stays unchanged) — see
    Design/Multiform Pokemon Reference Picker Plan.md.

    `PokeApiClient.get_species_forms` already degrades its own PokeAPI
    failures to a single-form result, but this wraps the call in a broad
    except anyway (and treats a client with no `get_species_forms`/
    `forms_button_view` the same way) so a forms lookup can never raise into
    `post_daily_for_guild`/`post_wild_encounter_for_guild` and delay or block
    that day's post. Reuses the bot's single persistent view instance
    (`client.forms_button_view`, registered once in `PokeSketchDexBot.setup_hook`)
    rather than constructing a new one per message, which would defeat
    cross-restart persistence.
    """
    try:
        forms = await api.get_species_forms(dex_no)
    except Exception:  # noqa: BLE001 - a forms-lookup failure must never block the post
        log.warning("Forms lookup failed for dex %s; posting without alt-forms button.", dex_no, exc_info=True)
        return None
    if not forms.has_alt_forms:
        return None
    return getattr(client, "forms_button_view", None)


def _wild_encounter_window_summary_line(posted_at_utc: datetime, catch_window_hours: int) -> str:
    """Same idea as _window_summary_line, but wild-encounter threads use a
    fixed WILD_ENCOUNTER_SUBMIT_WINDOW_HOURS submission window counted from
    this thread's own post time (`posted_at_utc`, naive-UTC) rather than a
    guild-local-midnight cutoff — see _find_wild_encounter_for_thread in
    submissions.py, which enforces this same deadline against
    WildEncounter.created_at."""
    submit_deadline = pokebox.discord_timestamp(
        posted_at_utc + timedelta(hours=WILD_ENCOUNTER_SUBMIT_WINDOW_HOURS), style="R"
    )
    catch_label = pokebox.format_duration_label(catch_window_hours)
    return (
        f"⏳ `/submit` closes for this wild encounter {submit_deadline} "
        f"({WILD_ENCOUNTER_SUBMIT_WINDOW_HOURS}h after posting) • "
        f"`/catch` closes {catch_label} after each sketch is submitted."
    )


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
        catch_window_hours = cfg.catch_window_hours
        tz_name = cfg.timezone

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

    forms_view = await _forms_view_for_dex(client, api, dex_no)

    role_mention = f"<@&{role_id}>" if role_id else ""
    embed = daily_embed(ref, shiny, images)
    msg = await channel.send(
        content=f"{role_mention} 🎨 A new Pokemon to sketch today!".strip(),
        embed=embed,
        view=forms_view,
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
            "today's Pokemon. React 👍 to upvote entries.\n"
            f"{_window_summary_line(local_date, grace_period_days, catch_window_hours, tz_name)}"
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
    """Post a fresh companion wild-encounter thread for today: one Pokemon
    per thread, created new every day (not a single continuous weekly
    thread). Draws from the guild's active WeeklyVote pool — "active" is the
    most recent WeeklyVote with a resolved dex pool (see
    weeklyvote.get_active_weekly_vote) so the previous cycle's pool keeps
    supplying daily encounters with no gap while this week's day-1/day-2
    votes are still in progress. Meant to be called right after
    post_daily_for_guild so the wild encounter reads as a companion to that
    day's main challenge.
    """
    async with db.session() as s:
        cfg = await s.get(db.GuildConfig, guild_id)
        if cfg is None or cfg.paused or not cfg.channel_id:
            return False
        wv = await weeklyvote.get_active_weekly_vote(s, guild_id)
        if wv is None:
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
        channel_id, weekly_vote_id = cfg.channel_id, wv.id
        catch_window_hours = cfg.catch_window_hours
        theme_category, theme_choice_key = wv.category, wv.choice_key

    dex_no = random.choice(dex_pool)
    ref = await api.get_pokemon(dex_no)
    images = ref.reference_images(shiny=False)

    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.DiscordException:
            log.warning("Guild %s: channel %s not found.", guild_id, channel_id)
            return False

    forms_view = await _forms_view_for_dex(client, api, dex_no)

    embed = daily_embed(ref, False, images)
    embed.title = f"🌿 Wild encounter: #{ref.dex_no:04d} {ref.display_name()}"
    if theme_category == weeklyvote.CATEGORY_TYPE and theme_choice_key:
        theme_emoji = weeklyvote.type_emoji(theme_choice_key)
        embed.set_footer(text=f"{theme_emoji} This week's theme: {theme_choice_key.title()}-type")
    # Captured once, right before posting, so the deadline shown in the
    # thread and the deadline enforced against WildEncounter.created_at (see
    # _find_wild_encounter_for_thread in submissions.py) line up exactly.
    posted_at = datetime.now(UTC).replace(tzinfo=None)
    msg = await channel.send(content="A wild Pokemon appeared!", embed=embed, view=forms_view)

    thread = None
    try:
        thread = await msg.create_thread(
            name=f"{local_date.isoformat()} Wild — {ref.display_name()}",
            auto_archive_duration=1440,  # 1 day — a fresh thread posts daily, no need to keep it open longer
        )
        await thread.send(
            "Sketch it with `/submit`!\n"
            f"{_wild_encounter_window_summary_line(posted_at, catch_window_hours)}"
        )
    except discord.DiscordException as exc:
        log.warning("Guild %s: could not create wild-encounter thread: %s", guild_id, exc)

    async with db.session() as s:
        s.add(
            db.WildEncounter(
                guild_id=guild_id,
                weekly_vote_id=weekly_vote_id,
                local_date=local_date,
                dex_no=ref.dex_no,
                name=ref.name,
                message_id=msg.id,
                thread_id=thread.id if thread else None,
                created_at=posted_at,
            )
        )
        await s.commit()
    log.info(
        "Guild %s: posted wild encounter #%s %s (thread=%s).",
        guild_id, ref.dex_no, ref.name, thread.id if thread else None,
    )
    return True
