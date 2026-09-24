"""PokeBox, Party & Pokeball business logic.

PokeBox is the free/unlimited dex-completion tracker (insert-if-not-exists,
called from /submit). CaughtMon is the capped, pokeball-gated collection of
cached sketch images: 6 active party slots + a 20-total storage cap, all
global (not per-guild). See the design doc for the full spec.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from math import ceil

import discord
import httpx
from PIL import Image, ImageOps
from sqlalchemy import desc, func, select
from sqlalchemy.orm import selectinload

from .db import CaughtMon, GlobalUser, GuildConfig, PokeballWallet, PokeBox, Submission, WildEncounterSubmission
from .formatting import species_display_name

log = logging.getLogger(__name__)

MAX_ACTIVE = 6
MAX_TOTAL = 20
TOTAL_DEX = 1025

# New players start with this many pokeballs (set at wallet creation, not the
# weekly grant) so they can /catch immediately without waiting for Monday.
STARTER_POKEBALLS = 5

# Cached party/box images are downsized to fit within this box (aspect
# preserved, never upscaled) to keep data/party_cache/ bounded regardless
# of how large the original Discord attachment was.
MAX_IMAGE_DIMENSION = 1080

# (generation label, dex_min, dex_max) — national dex generation boundaries.
GENERATIONS = [
    ("Gen 1 (Kanto)", 1, 151),
    ("Gen 2 (Johto)", 152, 251),
    ("Gen 3 (Hoenn)", 252, 386),
    ("Gen 4 (Sinnoh)", 387, 493),
    ("Gen 5 (Unova)", 494, 649),
    ("Gen 6 (Kalos)", 650, 721),
    ("Gen 7 (Alola)", 722, 809),
    ("Gen 8 (Galar)", 810, 905),
    ("Gen 9 (Paldea)", 906, 1025),
]


NICKNAME_MAX_LEN = 12
_NICKNAME_RE = re.compile(r"^[A-Za-z0-9 '.\-]+$")


class CatchError(Exception):
    """Expected, user-facing failure — caller should show `str(exc)` as a plain error."""


def sanitize_nickname(raw: str) -> str | None:
    """Validate and normalize a /catch nickname. Raises CatchError on rejection.

    Single choke point for nickname rules (e.g. a future language filter bolts
    on here without touching call sites). Rejects disallowed characters rather
    than silently stripping them, so a saved nickname never differs from what
    the player typed. Empty after stripping whitespace means "no nickname".
    """
    stripped = raw.strip()
    if not stripped:
        return None
    if len(stripped) > NICKNAME_MAX_LEN:
        raise CatchError(f"Nickname must be {NICKNAME_MAX_LEN} characters or fewer.")
    if not _NICKNAME_RE.match(stripped):
        raise CatchError(
            "Nickname can only contain letters, numbers, spaces, and ' . - "
            "(no backticks, @, #, <, >, :, or other special characters)."
        )
    return stripped


async def get_or_create_wallet(s, user_id: int) -> PokeballWallet:
    wallet = await s.get(PokeballWallet, user_id)
    if wallet is None:
        wallet = PokeballWallet(user_id=user_id, balance=STARTER_POKEBALLS)
        s.add(wallet)
        await s.flush()
    return wallet


async def record_pokebox_scan(s, user_id: int, dex_no: int, submission_id: int | None) -> None:
    """Insert-if-not-exists into PokeBox. Free, automatic — scans this dex number into the
    player's Pokédex, called from /submit."""
    existing = await s.get(PokeBox, (user_id, dex_no))
    if existing is None:
        s.add(PokeBox(user_id=user_id, dex_no=dex_no, submission_id=submission_id))


async def pokebox_progress(s, user_id: int) -> tuple[int, int]:
    """Return (scanned, total)."""
    scanned = (
        await s.execute(select(func.count()).select_from(PokeBox).where(PokeBox.user_id == user_id))
    ).scalar_one()
    return scanned, TOTAL_DEX


async def pokebox_by_generation(s, user_id: int) -> list[tuple[str, int, int]]:
    """Return [(label, scanned, total)] per generation."""
    dex_nos = set(
        (await s.execute(select(PokeBox.dex_no).where(PokeBox.user_id == user_id))).scalars().all()
    )
    out = []
    for label, lo, hi in GENERATIONS:
        scanned = sum(1 for d in dex_nos if lo <= d <= hi)
        out.append((label, scanned, hi - lo + 1))
    return out


def _naive_utcnow() -> datetime:
    """UTC now with tzinfo stripped.

    SQLite round-trips DateTime(timezone=True) columns as naive text (a
    SQLAlchemy+SQLite limitation), so `Submission.created_at` comes back
    naive even though it was written as UTC-aware — compare against this
    instead of an aware `datetime.now(UTC)` to avoid a naive/aware TypeError.
    """
    return datetime.now(UTC).replace(tzinfo=None)


CatchableSubmission = Submission | WildEncounterSubmission


def encode_catch_target(sub: CatchableSubmission) -> str:
    """Encode a catchable row into an opaque /catch target string.

    Submission and WildEncounterSubmission are separate tables with their own
    id sequences, so a bare id would be ambiguous between the two — prefix
    with the source so decode_catch_target can tell them apart.
    """
    prefix = "w" if isinstance(sub, WildEncounterSubmission) else "s"
    return f"{prefix}{sub.id}"


def decode_catch_target(raw: str) -> tuple[str, int] | None:
    """Inverse of encode_catch_target. Returns None for any malformed input
    (e.g. a stale value from before this encoding existed)."""
    if len(raw) < 2 or raw[0] not in ("s", "w"):
        return None
    try:
        return raw[0], int(raw[1:])
    except ValueError:
        return None


async def catchable_submissions(s, user_id: int) -> list[tuple[CatchableSubmission, int]]:
    """All of this user's eligible, not-yet-caught submissions across every guild
    and both the main-daily and wild-encounter threads (catching is
    global/cross-guild by design), most recent first.

    Each result is paired with that submission's own guild's catch_window_hours
    (a submission's eligibility is judged against its own guild's setting, not
    a single global value, since different guilds can configure different
    windows). Excludes submissions already pointed at by a CaughtMon (via
    source_submission_id or source_wild_encounter_submission_id), so the same
    sketch can't be caught twice.
    """
    now = _naive_utcnow()
    caught_daily_ids = set(
        (
            await s.execute(
                select(CaughtMon.source_submission_id).where(
                    CaughtMon.user_id == user_id, CaughtMon.source_submission_id.is_not(None)
                )
            )
        ).scalars().all()
    )
    caught_wild_ids = set(
        (
            await s.execute(
                select(CaughtMon.source_wild_encounter_submission_id).where(
                    CaughtMon.user_id == user_id, CaughtMon.source_wild_encounter_submission_id.is_not(None)
                )
            )
        ).scalars().all()
    )
    daily_rows = (
        await s.execute(
            select(Submission, GuildConfig.catch_window_hours)
            .join(GuildConfig, GuildConfig.guild_id == Submission.guild_id)
            .options(selectinload(Submission.daily))
            .where(Submission.user_id == user_id)
        )
    ).all()
    wild_rows = (
        await s.execute(
            select(WildEncounterSubmission, GuildConfig.catch_window_hours)
            .join(GuildConfig, GuildConfig.guild_id == WildEncounterSubmission.guild_id)
            .options(selectinload(WildEncounterSubmission.wild_encounter))
            .where(WildEncounterSubmission.user_id == user_id)
        )
    ).all()

    out: list[tuple[CatchableSubmission, int]] = []
    for sub, catch_window_hours in daily_rows:
        if sub.id in caught_daily_ids:
            continue
        if sub.created_at >= now - timedelta(hours=catch_window_hours):
            out.append((sub, catch_window_hours))
    for sub, catch_window_hours in wild_rows:
        if sub.id in caught_wild_ids:
            continue
        if sub.created_at >= now - timedelta(hours=catch_window_hours):
            out.append((sub, catch_window_hours))

    out.sort(key=lambda pair: pair[0].created_at, reverse=True)
    return out


def format_duration_label(hours: float) -> str:
    """Human label for a duration: \"Nh\" under 24h, else \"Nd\" (rounded up).

    Used for describing a fixed duration (e.g. "24h" as a catch window's
    length) — not a live countdown. Live/self-updating deadlines should use
    discord_timestamp() instead, which Discord renders client-side and keeps
    ticking without the bot re-editing anything.
    """
    if hours < 24:
        return f"{max(1, ceil(hours))}h"
    return f"{ceil(hours / 24)}d"


def discord_timestamp(dt: datetime, style: str = "R") -> str:
    """Render a naive-UTC datetime as Discord's <t:UNIX:style> markdown.

    Discord renders this client-side and keeps it live (style "R" = dynamic
    relative countdown, e.g. "in 3 hours" / "5 minutes ago"), correctly
    localized to each viewer's own timezone, with no bot-side re-editing
    needed. `dt` is assumed naive-UTC (the convention this module already
    uses for DB-round-tripped timestamps — see _naive_utcnow()).
    """
    epoch_seconds = int(dt.replace(tzinfo=UTC).timestamp())
    return f"<t:{epoch_seconds}:{style}>"


def catch_label_parts(sub: CatchableSubmission, catch_window_hours: int) -> tuple[str, str, str]:
    """Return (species display name, relative day label, time-left label) for a
    /catch autocomplete choice, e.g. ("Pikachu", "today", "18h left")."""
    now = _naive_utcnow()
    expires_at = sub.created_at + timedelta(hours=catch_window_hours)
    remaining_hours = (expires_at - now).total_seconds() / 3600
    time_left = f"{format_duration_label(remaining_hours)} left"
    created_date = sub.created_at.date()
    day_label = "today" if created_date == now.date() else f"{created_date:%b} {created_date.day}"
    return species_display_name(sub.species_name), day_label, time_left


def _next_free_slot(taken: set[int], upper: int) -> int | None:
    for i in range(1, upper + 1):
        if i not in taken:
            return i
    return None


async def _cache_image(url: str, user_id: int, caughtmon_id: int, cache_dir: str) -> str:
    user_dir = os.path.join(cache_dir, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    path = os.path.join(user_dir, f"{caughtmon_id}.png")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    await asyncio.to_thread(_normalize_and_save, resp.content, path)
    return path


def _normalize_and_save(raw: bytes, path: str, max_dim: int = MAX_IMAGE_DIMENSION) -> None:
    """Downsize an image to fit within max_dim x max_dim and save as PNG.

    Preserves aspect ratio, never upscales (a smaller original is left as
    is), and flattens to RGB/RGBA so re-saving as PNG is always safe
    regardless of the source format. Keeps data/party_cache/ bounded no
    matter how large the original Discord attachment was.
    """
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img) or img
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA" if "A" in img.mode else "RGB")
        if img.width > max_dim or img.height > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        img.save(path, format="PNG")


def delete_cached_file(path: str) -> None:
    if path:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


async def catch_submission(
    s,
    user_id: int,
    cache_dir: str,
    target: str | None = None,
    nickname: str | None = None,
) -> CaughtMon:
    """Spend 1 pokeball to catch an eligible, not-yet-caught submission.

    With no target, catches the user's single most recent eligible submission
    (same UX as before, widened from "today only" to each guild's catch
    window). With a target (an encode_catch_target string), always
    re-resolves it against the live eligible set at execution time — Discord
    doesn't guarantee a submitted `target` string actually came from the
    autocomplete list, and the window could have lapsed or a concurrent
    request could have caught it in between.

    Raises CatchError for any expected failure (no pokeballs, no eligible
    submission, stale/invalid target, bad nickname, storage full) — caller
    shows the message as a plain error.
    """
    wallet = await get_or_create_wallet(s, user_id)
    if wallet.balance <= 0:
        raise CatchError("No pokeballs left. Check `/pokeballs` for your next weekly grant.")

    eligible = await catchable_submissions(s, user_id)
    if target is None:
        if not eligible:
            raise CatchError("No catchable sketches right now — `/submit` first, then `/catch`.")
        sub, _ = eligible[0]
    else:
        decoded = decode_catch_target(target)
        match = None
        if decoded is not None:
            kind, sub_id = decoded
            source_type = WildEncounterSubmission if kind == "w" else Submission
            match = next((e for e in eligible if e[0].id == sub_id and isinstance(e[0], source_type)), None)
        if match is None:
            raise CatchError(
                "That sketch is no longer catchable — it may be outside the window or "
                "already caught. Run `/catch` again to see current options."
            )
        sub, _ = match

    clean_nickname = sanitize_nickname(nickname) if nickname is not None else None

    rows = (await s.execute(select(CaughtMon).where(CaughtMon.user_id == user_id))).scalars().all()
    if len(rows) >= MAX_TOTAL:
        raise CatchError(f"Storage full ({MAX_TOTAL}/{MAX_TOTAL}) — release a mon first with `/release`.")

    active_slots = {r.slot for r in rows if r.is_active}
    is_active = len(active_slots) < MAX_ACTIVE
    if is_active:
        slot = _next_free_slot(active_slots, MAX_ACTIVE)
    else:
        box_slots = {r.slot for r in rows if not r.is_active}
        slot = _next_free_slot(box_slots, MAX_TOTAL)
    if slot is None:
        # Capacity was already checked above via len(rows); this is unreachable.
        raise CatchError(f"Storage full ({MAX_TOTAL}/{MAX_TOTAL}) — release a mon first with `/release`.")

    mon = CaughtMon(
        user_id=user_id,
        is_active=is_active,
        slot=slot,
        dex_no=sub.dex_no,
        name=sub.species_name,
        is_shiny=sub.is_shiny,
        nickname=clean_nickname,
        cached_image_path="",
        source_submission_id=sub.id if isinstance(sub, Submission) else None,
        source_wild_encounter_submission_id=sub.id if isinstance(sub, WildEncounterSubmission) else None,
    )
    s.add(mon)
    await s.flush()  # assign mon.id for the cache filename

    mon.cached_image_path = await _cache_image(sub.image_url, user_id, mon.id, cache_dir)
    wallet.balance -= 1
    return mon


async def swap_mon(s, user_id: int, box_slot: int, active_slot: int) -> tuple[CaughtMon, CaughtMon | None]:
    """Move a boxed mon into an active slot, demoting whatever was there to storage.

    Returns (promoted_mon, demoted_mon_or_None).
    """
    if not (1 <= active_slot <= MAX_ACTIVE):
        raise CatchError(f"Active slot must be 1-{MAX_ACTIVE}.")
    if not (1 <= box_slot <= MAX_TOTAL):
        raise CatchError(f"Box slot must be 1-{MAX_TOTAL}.")

    boxed = (
        await s.execute(
            select(CaughtMon).where(
                CaughtMon.user_id == user_id, ~CaughtMon.is_active, CaughtMon.slot == box_slot
            )
        )
    ).scalar_one_or_none()
    if boxed is None:
        raise CatchError(f"No boxed mon at slot {box_slot}.")

    active = (
        await s.execute(
            select(CaughtMon).where(
                CaughtMon.user_id == user_id, CaughtMon.is_active, CaughtMon.slot == active_slot
            )
        )
    ).scalar_one_or_none()

    old_box_slot = boxed.slot
    boxed.is_active = True
    boxed.slot = active_slot
    if active is not None:
        active.is_active = False
        active.slot = old_box_slot
    return boxed, active


async def release_mon(s, user_id: int, slot: int, is_active: bool) -> str:
    """Delete a CaughtMon row. Returns its cached image path for the caller to
    remove from disk *after* the transaction commits."""
    upper = MAX_ACTIVE if is_active else MAX_TOTAL
    if not (1 <= slot <= upper):
        raise CatchError(f"Slot must be 1-{upper}.")
    mon = (
        await s.execute(
            select(CaughtMon).where(
                CaughtMon.user_id == user_id, CaughtMon.is_active == is_active, CaughtMon.slot == slot
            )
        )
    ).scalar_one_or_none()
    if mon is None:
        kind = "active" if is_active else "box"
        raise CatchError(f"No mon in {kind} slot {slot}.")
    path = mon.cached_image_path
    await s.delete(mon)
    return path


async def total_caught_count(s, user_id: int) -> int:
    """Total CaughtMon rows (active + boxed) for a user — the /20 cap."""
    return (
        await s.execute(select(func.count()).select_from(CaughtMon).where(CaughtMon.user_id == user_id))
    ).scalar_one()


async def party_listing(s, user_id: int) -> list[CaughtMon]:
    return (
        await s.execute(
            select(CaughtMon)
            .where(CaughtMon.user_id == user_id, CaughtMon.is_active)
            .order_by(CaughtMon.slot)
        )
    ).scalars().all()


async def box_listing(s, user_id: int) -> list[CaughtMon]:
    return (
        await s.execute(
            select(CaughtMon)
            .where(CaughtMon.user_id == user_id, ~CaughtMon.is_active)
            .order_by(CaughtMon.slot)
        )
    ).scalars().all()


def build_party_embeds(display_name: str, mons: list[CaughtMon]) -> tuple[list[discord.Embed], list[discord.File]]:
    """Build the (embeds, files) pair for a party display — shared by /party
    (cogs/collection.py) and the merged /profile command's "Inspect Party"
    button (cogs/profile.py), so both surfaces render a party the same way
    instead of two divergent implementations of the same listing."""
    embeds: list[discord.Embed] = []
    files: list[discord.File] = []
    for mon in mons:
        embed = discord.Embed(title=f"Slot {mon.slot}: {_party_display_name(mon)}", color=0x5865F2)
        if mon.cached_image_path and os.path.exists(mon.cached_image_path):
            filename = f"slot{mon.slot}.png"
            files.append(discord.File(mon.cached_image_path, filename=filename))
            embed.set_image(url=f"attachment://{filename}")
        embeds.append(embed)
    if embeds:
        embeds[0].set_author(name=f"{display_name}'s Active Party ({len(mons)}/{MAX_ACTIVE})")
    return embeds, files


def _party_display_name(mon: CaughtMon) -> str:
    shiny_tag = " ✨" if mon.is_shiny else ""
    species = f"#{mon.dex_no:04d} {species_display_name(mon.name)}{shiny_tag}"
    if mon.nickname:
        return f"{mon.nickname} ({species})"
    return species


async def shiny_summary(s, user_id: int) -> tuple[int, str | None]:
    """Return (distinct shiny species count, most-recently-caught shiny's
    display name or None) across a user's whole collection (active party +
    storage box — "have you ever caught a shiny", not just their current
    active 6)."""
    rows = (
        await s.execute(
            select(CaughtMon)
            .where(CaughtMon.user_id == user_id, CaughtMon.is_shiny)
            .order_by(desc(CaughtMon.caught_at))
        )
    ).scalars().all()
    if not rows:
        return 0, None
    distinct_species = len({r.dex_no for r in rows})
    return distinct_species, species_display_name(rows[0].name)


async def days_since_first_submission(s, user_id: int) -> int | None:
    """Whole days elapsed since a user's first-ever Submission (any guild),
    or None if they have never submitted. Used to compute accuracy% on the
    merged /profile command — MIN(Submission.created_at) is the literal
    "days since joining" source, more direct than trusting GlobalUser.created_at
    (set once at first insert and never touched, but a step removed from the
    actual first-submission event)."""
    first_created_at = (
        await s.execute(select(func.min(Submission.created_at)).where(Submission.user_id == user_id))
    ).scalar_one_or_none()
    if first_created_at is None:
        return None
    return max(0, (_naive_utcnow() - first_created_at).days)


def week_key(dt: datetime | None = None) -> str:
    """ISO week string, e.g. '2026-W39', for idempotent weekly grants."""
    dt = dt or datetime.now(UTC)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def weekly_grant_amount(level: int) -> int:
    return 1 + level // 3


def next_weekly_grant_at(now: datetime | None = None) -> datetime:
    """Next Monday 00:00 UTC — matches the scheduled weekly grant job."""
    now = now or datetime.now(UTC)
    target = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = (7 - target.weekday()) % 7  # Monday == 0
    if days_ahead == 0 and now > target:
        days_ahead = 7
    return target + timedelta(days=days_ahead)


async def grant_weekly_pokeballs(s, now: datetime | None = None) -> int:
    """Grant weekly pokeballs to every GlobalUser. Idempotent per ISO week
    via PokeballWallet.last_granted_week. Returns the number of users granted."""
    week = week_key(now)
    users = (await s.execute(select(GlobalUser))).scalars().all()
    granted = 0
    for gu in users:
        wallet = await get_or_create_wallet(s, gu.user_id)
        if wallet.last_granted_week == week:
            continue
        wallet.balance += weekly_grant_amount(gu.level)
        wallet.last_granted_week = week
        granted += 1
    return granted
