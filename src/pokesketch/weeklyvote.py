"""Weekly two-stage vote (category -> specific choice) and pool resolution.

Day 1: a native Discord Poll asks which *category* (Location/Type/Event/
Habitat/Generation) this week's wild encounters draw from. Day 2 (the day
after): a second poll asks the specific choice within that category. Once
day 2 closes, the winning choice resolves to a list of dex numbers cached on
`WeeklyVote.resolved_dex_pool`, and a wild-encounter thread is created.

Poll-close detection is scheduled-job-driven, not webhook-driven: this bot
has no webserver/interaction-event infra (everything else is APScheduler
cron jobs — see bot.py), and discord.py's Poll API has no distinct "poll
closed" gateway event. Each job fetches the previous poll's message, force-
ends it if Discord hasn't already closed it, and reads the results directly.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

import discord
from sqlalchemy import select

from . import db, pokebox
from .db import EventDefinition, GuildConfig, HabitatPool, LocationArea, TypePool, WeeklyVote
from .pokeapi import PokeApiClient

log = logging.getLogger(__name__)

CATEGORY_LOCATION = "location"
CATEGORY_TYPE = "type"
CATEGORY_EVENT = "event"
CATEGORY_HABITAT = "habitat"
CATEGORY_GENERATION = "generation"

ALL_CATEGORIES = [CATEGORY_LOCATION, CATEGORY_TYPE, CATEGORY_EVENT, CATEGORY_HABITAT, CATEGORY_GENERATION]

CATEGORY_LABELS = {
    CATEGORY_LOCATION: "📍 Location",
    CATEGORY_TYPE: "🔥 Type",
    CATEGORY_EVENT: "🎉 Event",
    CATEGORY_HABITAT: "🌲 Habitat",
    CATEGORY_GENERATION: "🗺️ Generation/Region",
}

# Discord polls support at most 10 answers, so any category whose full
# candidate set is bigger than this gets randomly sampled down for the day-2
# ballot (Type has 18 canonical types; an admin could author >10 events).
MAX_POLL_OPTIONS = 10
# Discord's actual per-answer text cap.
_POLL_ANSWER_MAX_LEN = 55

# Discord auto-closes polls after their duration. We use 48h so the bot's
# explicit force-end (scheduled job) is always what ends a vote; 24h could
# expire before the next daily job across a DST boundary or misfire grace.
VOTE_POLL_DURATION = timedelta(hours=48)

TYPE_NAMES = [
    "normal", "fire", "water", "electric", "grass", "ice", "fighting", "poison", "ground",
    "flying", "psychic", "bug", "rock", "ghost", "dragon", "dark", "steel", "fairy",
]

# Flavor emoji per type, shown on the day-2 ballot answers and on the wild-
# encounter announcement once a Type category wins, so "this week's theme"
# reads at a glance instead of as bare text.
TYPE_EMOJI = {
    "normal": "⚪️",
    "fire": "🔥",
    "water": "💧",
    "electric": "⚡️",
    "grass": "🌼",
    "ice": "❄️",
    "fighting": "💪",
    "poison": "⚠️",
    "ground": "⛰️",
    "flying": "🪽",
    "psychic": "🔮",
    "bug": "🕷",
    "rock": "🪨",
    "ghost": "👻",
    "dragon": "🐲",
    "dark": "🌑",
    "steel": "⚙️",
    "fairy": "🧚",
}


def type_emoji(type_name: str) -> str:
    """Emoji for a type name, or 🌟 as a safe fallback for unrecognized input."""
    return TYPE_EMOJI.get(type_name.lower(), "🌟")

HABITAT_NAMES = ["cave", "forest", "grassland", "mountain", "rare", "rough-terrain", "sea", "urban", "waters-edge"]

# Curated (PokeAPI location-area id, slug, display name). PokeAPI has
# thousands of location-areas of wildly varying naming/data quality; this is
# a hand-picked, verified-nonempty sample spanning most generations. Gen 9
# (Paldea) location-areas are thin/inconsistently named on PokeAPI (see
# pokeapi/pokeapi#958) so there's no curated Paldea entry yet.
CURATED_LOCATION_AREAS: list[tuple[int, str, str]] = [
    (295, "kanto-route-1-area", "Kanto Route 1"),
    (313, "kanto-route-22-area", "Kanto Route 22"),
    (321, "viridian-forest-area", "Viridian Forest"),
    (290, "mt-moon-1f", "Mt. Moon"),
    (187, "johto-route-30-area", "Johto Route 30"),
    (393, "hoenn-route-101-area", "Hoenn Route 101"),
    (141, "sinnoh-route-201-area", "Sinnoh Route 201"),
    (9, "eterna-forest-area", "Eterna Forest"),
    (623, "unova-route-1-area", "Unova Route 1"),
    (713, "kalos-route-2-area", "Kalos Route 2"),
    (843, "galar-route-1-area", "Galar Route 1"),
]


def _habitat_display(habitat_name: str) -> str:
    return habitat_name.replace("-", " ").title()


def _join_dex_pool(dex_nos: list[int]) -> str:
    return "\n".join(str(d) for d in dex_nos)


async def active_event_definitions(s, guild_id: int) -> list[EventDefinition]:
    return (
        await s.execute(
            select(EventDefinition).where(EventDefinition.guild_id == guild_id, EventDefinition.is_active)
        )
    ).scalars().all()


async def build_category_ballot(s, guild_id: int) -> list[str]:
    """All 5 categories, minus Event if the guild has zero active EventDefinitions."""
    cats = list(ALL_CATEGORIES)
    if not await active_event_definitions(s, guild_id):
        cats.remove(CATEGORY_EVENT)
    return cats


def _sample_candidates(candidates: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if len(candidates) <= MAX_POLL_OPTIONS:
        return candidates
    return random.sample(candidates, MAX_POLL_OPTIONS)


async def _get_type_pool(s, api: PokeApiClient, type_name: str) -> list[int]:
    row = await s.get(TypePool, type_name)
    if row is not None:
        return row.dex_numbers
    dex_nos = await api.get_type_pokemon_dex_nos(type_name)
    s.add(TypePool(key=type_name, name=type_name, display_name=type_name.title(), dex_pool=_join_dex_pool(dex_nos)))
    await s.flush()
    return dex_nos


async def _get_habitat_pool(s, api: PokeApiClient, habitat_name: str) -> list[int]:
    row = await s.get(HabitatPool, habitat_name)
    if row is not None:
        return row.dex_numbers
    dex_nos = await api.get_habitat_pokemon_dex_nos(habitat_name)
    s.add(
        HabitatPool(
            key=habitat_name, name=habitat_name, display_name=_habitat_display(habitat_name),
            dex_pool=_join_dex_pool(dex_nos),
        )
    )
    await s.flush()
    return dex_nos


async def _get_location_area_pool(s, api: PokeApiClient, area_id: int, slug: str, display: str) -> list[int]:
    row = await s.get(LocationArea, area_id)
    if row is not None:
        return row.dex_numbers
    dex_nos = await api.get_location_area_encounters(slug)
    s.add(LocationArea(id=area_id, name=slug, display_name=display, dex_pool=_join_dex_pool(dex_nos)))
    await s.flush()
    return dex_nos


async def _day2_candidates(s, api: PokeApiClient, guild_id: int, category: str) -> list[tuple[str, str]]:
    """Return [(poll display text, choice_key)] for the winning category.

    display text and choice_key are deliberately kept equal (or trivially
    derivable from each other) for every category so the day-2 resolution
    job can recover choice_key from the winning poll answer's text without
    needing to persist a separate mapping between the two pipeline runs.
    """
    if category == CATEGORY_TYPE:
        return [(f"{type_emoji(t)} {t.title()}", t) for t in TYPE_NAMES]
    if category == CATEGORY_HABITAT:
        return [(_habitat_display(h), h) for h in HABITAT_NAMES]
    if category == CATEGORY_GENERATION:
        return [(label, label) for label, _lo, _hi in pokebox.GENERATIONS]
    if category == CATEGORY_EVENT:
        events = await active_event_definitions(s, guild_id)
        return [(e.name, e.name) for e in events]
    if category == CATEGORY_LOCATION:
        out = []
        for area_id, slug, display in CURATED_LOCATION_AREAS:
            pool = await _get_location_area_pool(s, api, area_id, slug, display)
            if pool:  # skip areas with no encounter data (e.g. Gen9 coverage gap)
                out.append((display, display))
        return out
    raise ValueError(f"unknown category {category!r}")


async def resolve_dex_pool(s, api: PokeApiClient, guild_id: int, category: str, choice_key: str) -> list[int]:
    """Resolve a winning category/choice to a list of dex numbers, populating
    the relevant cache table (Type/Habitat/LocationArea) if needed."""
    if category == CATEGORY_TYPE:
        # choice_key here is the raw winning poll-answer text, e.g.
        # "⚙️ Steel" (see _day2_candidates: display text and choice_key are
        # NOT equal for Type, unlike every other category) -- strip the
        # leading emoji and title-casing back down to the bare type name
        # PokeAPI expects, or every Type-category resolution 400s.
        return await _get_type_pool(s, api, choice_key.split(" ", 1)[-1].strip().lower())
    if category == CATEGORY_HABITAT:
        return await _get_habitat_pool(s, api, choice_key.lower().replace(" ", "-"))
    if category == CATEGORY_GENERATION:
        for label, lo, hi in pokebox.GENERATIONS:
            if label == choice_key:
                return list(range(lo, hi + 1))
        return []
    if category == CATEGORY_EVENT:
        event = (
            await s.execute(
                select(EventDefinition).where(EventDefinition.guild_id == guild_id, EventDefinition.name == choice_key)
            )
        ).scalar_one_or_none()
        return event.dex_numbers if event else []
    if category == CATEGORY_LOCATION:
        for area_id, slug, display in CURATED_LOCATION_AREAS:
            if display == choice_key:
                return await _get_location_area_pool(s, api, area_id, slug, display)
        return []
    return []


async def get_active_weekly_vote(s, guild_id: int) -> WeeklyVote | None:
    """The WeeklyVote currently supplying the wild-encounter pool: the most
    recent row with a resolved dex pool (not "this calendar week's" — day-2
    resolution isn't guaranteed to land at a fixed time, so the *previous*
    cycle's pool must keep serving until the new one actually resolves, with
    no gap). Note this no longer has anything to do with `thread_id`, which
    is now per-day (see WildEncounter.thread_id) rather than per-week."""
    return (
        await s.execute(
            select(WeeklyVote)
            .where(WeeklyVote.guild_id == guild_id, WeeklyVote.resolved_dex_pool.is_not(None))
            .order_by(WeeklyVote.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _close_and_get_winner(channel: discord.abc.Messageable, message_id: int) -> str | None:
    """Fetch a poll message, force-end it if Discord hasn't already closed it,
    and return the winning answer's text (ties broken by answer order)."""
    try:
        msg = await channel.fetch_message(message_id)
    except discord.DiscordException:
        log.warning("Poll message %s not found; cannot resolve winner.", message_id)
        return None
    poll = msg.poll
    if poll is None:
        return None
    if not poll.is_finalized():
        try:
            poll = await poll.end()
        except discord.DiscordException as exc:
            log.warning("Could not end poll %s: %s", message_id, exc)
    if not poll.answers:
        return None
    winner = max(poll.answers, key=lambda a: a.vote_count)
    return winner.text


async def _fetch_channel(client: discord.Client, channel_id: int) -> discord.abc.GuildChannel:
    channel = client.get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    return channel


async def post_category_poll(client: discord.Client, guild_id: int) -> bool:
    """Day-1 job: post the category ballot poll and create the WeeklyVote row."""
    async with db.session() as s:
        cfg = await s.get(GuildConfig, guild_id)
        if cfg is None or cfg.paused or not cfg.channel_id:
            return False
        iso_week = pokebox.week_key()
        existing = (
            await s.execute(
                select(WeeklyVote).where(WeeklyVote.guild_id == guild_id, WeeklyVote.iso_week == iso_week)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return False  # idempotent: already posted this week
        ballot = await build_category_ballot(s, guild_id)
        channel_id = cfg.channel_id

    if len(ballot) < 2:
        log.warning("Guild %s: category ballot has <2 options; skipping vote this week.", guild_id)
        return False

    channel = await _fetch_channel(client, channel_id)
    poll = discord.Poll(question="This week's wild encounter category?", duration=VOTE_POLL_DURATION)
    for cat in ballot:
        poll.add_answer(text=CATEGORY_LABELS[cat])
    msg = await channel.send(content="🗳️ Vote for this week's wild encounter theme!", poll=poll)

    async with db.session() as s:
        s.add(WeeklyVote(guild_id=guild_id, iso_week=iso_week, category_poll_message_id=msg.id))
        await s.commit()
    log.info("Guild %s: posted category poll for %s (%s).", guild_id, iso_week, ballot)
    return True


async def _finalize_weekly_vote(
    client: discord.Client, api: PokeApiClient, guild_id: int, weekly_vote_id: int, category: str, choice_key: str
) -> bool:
    """Resolve resolved_dex_pool for the winning category/choice and stamp
    choice_key. No thread is created here — wild-encounter threads are now
    one-per-day, created fresh each day by post_wild_encounter_for_guild
    (see daily.py) once resolved_dex_pool is set. Shared by the normal
    day-2-resolution path and the "<2 valid choices" auto-resolve edge case
    (no poll posted)."""
    async with db.session() as s:
        cfg = await s.get(GuildConfig, guild_id)
        if cfg is None or not cfg.channel_id:
            return False
        dex_pool = await resolve_dex_pool(s, api, guild_id, category, choice_key)
        await s.commit()  # persist any newly-cached pool rows

    if not dex_pool:
        log.warning(
            "Guild %s: resolved empty dex pool for %s/%s; skipping.",
            guild_id, category, choice_key,
        )
        return False

    async with db.session() as s:
        wv = await s.get(WeeklyVote, weekly_vote_id)
        wv.choice_key = choice_key
        wv.resolved_dex_pool = _join_dex_pool(dex_pool)
        await s.commit()
    log.info("Guild %s: resolved %s/%s -> %d dex.", guild_id, category, choice_key, len(dex_pool))
    return True


async def resolve_category_poll(client: discord.Client, api: PokeApiClient, guild_id: int) -> bool:
    """Day-2 job: read the day-1 winner, build+post the day-2 ballot (or
    auto-resolve immediately if the winning category has <2 valid choices)."""
    async with db.session() as s:
        cfg = await s.get(GuildConfig, guild_id)
        if cfg is None or not cfg.channel_id:
            return False
        # Look up the most recent row awaiting day-1 resolution directly,
        # rather than recomputing "today's" ISO week: day1 (Sunday) and day2
        # (Monday) straddle the ISO week boundary (weeks start Monday), so
        # re-deriving iso_week here can point at a week with no row yet and
        # silently miss the poll posted the day before.
        wv = (
            await s.execute(
                select(WeeklyVote)
                .where(
                    WeeklyVote.guild_id == guild_id,
                    WeeklyVote.category_poll_message_id.is_not(None),
                    WeeklyVote.category.is_(None),
                )
                .order_by(WeeklyVote.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if wv is None or wv.category_poll_message_id is None:
            return False  # no day-1 poll to resolve, or already resolved
        channel_id, message_id, weekly_vote_id = cfg.channel_id, wv.category_poll_message_id, wv.id

    channel = await _fetch_channel(client, channel_id)
    winner_label = await _close_and_get_winner(channel, message_id)
    if winner_label is None:
        return False
    category = {v: k for k, v in CATEGORY_LABELS.items()}.get(winner_label)
    if category is None:
        log.warning("Guild %s: category poll winner %r didn't match any known category.", guild_id, winner_label)
        return False

    async with db.session() as s:
        wv = await s.get(WeeklyVote, weekly_vote_id)
        wv.category = category
        candidates = _sample_candidates(await _day2_candidates(s, api, guild_id, category))
        await s.commit()

    if len(candidates) < 2:
        if not candidates:
            log.warning(
                "Guild %s: category %s has zero valid choices; nothing to resolve this cycle.",
                guild_id, category,
            )
            return True
        # Edge case: a single valid choice isn't a meaningful poll — skip straight to resolution.
        return await _finalize_weekly_vote(client, api, guild_id, weekly_vote_id, category, candidates[0][1])

    poll = discord.Poll(
        question=f"Day 2: pick this week's {CATEGORY_LABELS[category]}!"[:300], duration=VOTE_POLL_DURATION
    )
    for display_text, _choice_key in candidates:
        poll.add_answer(text=display_text[:_POLL_ANSWER_MAX_LEN])
    msg = await channel.send(content="🗳️ Vote for this week's specific wild encounter pool!", poll=poll)

    async with db.session() as s:
        wv = await s.get(WeeklyVote, weekly_vote_id)
        wv.choice_poll_message_id = msg.id
        await s.commit()
    log.info("Guild %s: posted choice poll for %s (%d options).", guild_id, category, len(candidates))
    return True


async def resolve_choice_poll(client: discord.Client, api: PokeApiClient, guild_id: int) -> bool:
    """Day-2-resolution job: read the choice-poll winner and finalize the week."""
    async with db.session() as s:
        cfg = await s.get(GuildConfig, guild_id)
        if cfg is None or not cfg.channel_id:
            return False
        # Same ISO-week-boundary hazard as resolve_category_poll: find the
        # most recent row awaiting day-2 resolution directly instead of
        # recomputing "today's" ISO week.
        wv = (
            await s.execute(
                select(WeeklyVote)
                .where(
                    WeeklyVote.guild_id == guild_id,
                    WeeklyVote.choice_poll_message_id.is_not(None),
                    WeeklyVote.resolved_dex_pool.is_(None),
                )
                .order_by(WeeklyVote.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if wv is None or wv.choice_poll_message_id is None:
            return False  # no day-2 poll to resolve, or already resolved (e.g. auto-resolved)
        channel_id, message_id = cfg.channel_id, wv.choice_poll_message_id
        weekly_vote_id, category = wv.id, wv.category

    channel = await _fetch_channel(client, channel_id)
    winner_text = await _close_and_get_winner(channel, message_id)
    if winner_text is None:
        return False
    return await _finalize_weekly_vote(client, api, guild_id, weekly_vote_id, category, winner_text)
