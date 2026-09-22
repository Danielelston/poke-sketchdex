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
from datetime import UTC, datetime, timedelta

import httpx
from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .db import CaughtMon, GlobalUser, PokeballWallet, PokeBox, Submission

log = logging.getLogger(__name__)

MAX_ACTIVE = 6
MAX_TOTAL = 20
TOTAL_DEX = 1025

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


class CatchError(Exception):
    """Expected, user-facing failure — caller should show `str(exc)` as a plain error."""


async def get_or_create_wallet(s, user_id: int) -> PokeballWallet:
    wallet = await s.get(PokeballWallet, user_id)
    if wallet is None:
        wallet = PokeballWallet(user_id=user_id)
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


async def _todays_submission(s, user_id: int) -> Submission | None:
    """Most recent submission by this user created today (UTC) — the /catch window."""
    return (
        await s.execute(
            select(Submission)
            .options(selectinload(Submission.daily))
            .where(
                Submission.user_id == user_id,
                func.date(Submission.created_at) == func.date(func.now()),
            )
            .order_by(Submission.id.desc())
        )
    ).scalars().first()


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


async def catch_todays_submission(s, user_id: int, cache_dir: str) -> CaughtMon:
    """Spend 1 pokeball to catch today's just-submitted Pokemon.

    Raises CatchError for any expected failure (no pokeballs, no submission
    today, storage full) — caller shows the message as a plain error.
    """
    wallet = await get_or_create_wallet(s, user_id)
    if wallet.balance <= 0:
        raise CatchError("No pokeballs left. Check `/pokeballs` for your next weekly grant.")

    sub = await _todays_submission(s, user_id)
    if sub is None:
        raise CatchError("No sketch submitted today yet — `/submit` first, then `/catch`.")

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
        dex_no=sub.daily.dex_no,
        name=sub.daily.name,
        is_shiny=sub.daily.is_shiny,
        cached_image_path="",
        source_submission_id=sub.id,
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
