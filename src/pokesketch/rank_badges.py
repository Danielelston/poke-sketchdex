"""Poké Ball rank badge tiers: a pure cosmetic derivation off `GlobalUser.level`.

Deliberately not the same table/concept as `GymBadge` (event trophies, guild-
scoped, already shipped) — see that model's own docstring for why these must
never share a table.

Also owns fetching + caching the ball item art from the public PokeAPI/sprites
GitHub repo, and pre-rendering the two per-tier card assets (a small opaque
corner emblem + a full-bleed alpha-faded background) — the one piece of
pre-rendering locked in by the design doc. Everything else about a profile
card (text, party thumbnails) is rendered fresh per call by `cards.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from PIL import Image

log = logging.getLogger(__name__)

SPRITES_BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/items"
USER_AGENT = "PokeSketchDex-Bot/0.1 (+https://github.com/Danielelston/poke-sketchdex)"

CORNER_EMBLEM_SIZE = 64
FULL_BLEED_OPACITY = 0.18
# How far the full-bleed art is upscaled past "just barely covers the canvas" —
# a purely aesthetic choice so the ball art bleeds off all four edges rather
# than exactly touching them.
FULL_BLEED_OVERSCAN = 1.4
# Flat silhouette fill color for the full-bleed watermark (see _render_tier_assets
# for why this isn't the raw sprite's own colors).
FULL_BLEED_SILHOUETTE_COLOR = (255, 255, 255)

# (level threshold, display name, PokeAPI/sprites item slug), ascending.
#
# Thresholds are tuned against leveling.exp_to_reach()'s quadratic curve
# (25 * (L-1) * L), not chosen arbitrarily. For a reasonably active player
# (~10 EXP/submit + up to +20/day streak bonus once their streak matures,
# i.e. roughly 20-30 EXP/day), the first three tiers land at roughly 3 weeks
# / ~3 months / ~7 months — frequent enough early wins to feel like real
# progress. The last two are deliberately long-run prestige goals (~1 and
# ~2+ years at that pace), matching a long-lived Discord community rather
# than a fast mobile-game curve — Master Ball as the rank badge should feel
# as rare as it does in the games.
RANK_TIERS: list[tuple[int, str, str]] = [
    (1, "Poké Ball", "poke-ball"),
    (5, "Great Ball", "great-ball"),
    (12, "Ultra Ball", "ultra-ball"),
    (20, "Premier Ball", "premier-ball"),
    (30, "Luxury Ball", "luxury-ball"),
    (45, "Master Ball", "master-ball"),
]


def rank_for_level(level: int) -> str:
    """Highest tier whose threshold level is <= `level` (levels below the
    first threshold, including non-positive levels, get the base tier)."""
    name = RANK_TIERS[0][1]
    for threshold, tier_name, _slug in RANK_TIERS:
        if level >= threshold:
            name = tier_name
        else:
            break
    return name


def _slug_for_tier(tier_name: str) -> str:
    for _threshold, name, slug in RANK_TIERS:
        if name == tier_name:
            return slug
    raise ValueError(f"Unknown rank tier: {tier_name!r}")


async def fetch_ball_sprite(tier_name: str, cache_dir: str) -> str:
    """Fetch (once) + cache-to-disk-forever the raw ball item PNG for a tier.

    Mirrors pokeapi.py's PokeApiClient caching pattern (check disk first,
    only hit the network on a miss, never re-fetch afterward) — ball art
    never changes underneath a cached response. Returns the local file path.
    """
    slug = _slug_for_tier(tier_name)
    raw_dir = os.path.join(cache_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    path = os.path.join(raw_dir, f"{slug}.png")
    if os.path.exists(path):
        return path
    url = f"{SPRITES_BASE}/{slug}.png"
    log.info("Fetching ball sprite: %s", url)
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    with open(path, "wb") as fh:
        fh.write(resp.content)
    return path


def _render_tier_assets(
    raw_path: str, canvas_size: tuple[int, int], corner_path: str, full_bleed_path: str
) -> None:
    """Composite both static tier assets from the raw ball PNG. Synchronous —
    run via asyncio.to_thread by the caller, same posture as pokebox.py's
    _normalize_and_save (Pillow calls block, so keep them off the event loop).

    The corner emblem uses the sprite's own painted colors (crisp enough at
    64px). The full-bleed background does NOT — PokeAPI/sprites' item art is
    only ~30x30px, so blowing that raw detail up ~30x for a 900px canvas (even
    at low opacity) just reads as a soft colored blur, not a recognizable ball
    shape (confirmed by manual visual review of sample renders before merge).
    Instead, the full-bleed layer uses the sprite's ALPHA MASK ONLY as a flat
    white silhouette — the mask's edges upscale cleanly via LANCZOS (shape,
    not fine color detail, survives the scale-up), giving a watermark that
    actually reads as a Poké Ball outline at low opacity.
    """
    with Image.open(raw_path) as raw:
        raw = raw.convert("RGBA")

        corner = Image.new("RGBA", (CORNER_EMBLEM_SIZE, CORNER_EMBLEM_SIZE), (0, 0, 0, 0))
        thumb = raw.copy()
        thumb.thumbnail((CORNER_EMBLEM_SIZE, CORNER_EMBLEM_SIZE), Image.Resampling.LANCZOS)
        offset = ((CORNER_EMBLEM_SIZE - thumb.width) // 2, (CORNER_EMBLEM_SIZE - thumb.height) // 2)
        corner.alpha_composite(thumb, offset)
        corner.save(corner_path, format="PNG")

        canvas_w, canvas_h = canvas_size
        bg = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        scale = max(canvas_w / raw.width, canvas_h / raw.height) * FULL_BLEED_OVERSCAN
        target_size = (max(1, int(raw.width * scale)), max(1, int(raw.height * scale)))
        # Upscale the ALPHA MASK (shape), not the raw RGBA (fine color detail),
        # then flat-fill it — this is what keeps the watermark legible at 30x.
        alpha_mask = raw.split()[3].resize(target_size, Image.Resampling.LANCZOS)
        silhouette = Image.new("RGBA", target_size, (*FULL_BLEED_SILHOUETTE_COLOR, 0))
        faded_alpha = alpha_mask.point(lambda a: int(a * FULL_BLEED_OPACITY))
        silhouette.putalpha(faded_alpha)
        offset = ((canvas_w - silhouette.width) // 2, (canvas_h - silhouette.height) // 2)
        bg.alpha_composite(silhouette, offset)
        bg.save(full_bleed_path, format="PNG")


async def get_tier_assets(tier_name: str, canvas_size: tuple[int, int], cache_dir: str) -> tuple[str, str]:
    """Return (corner_emblem_path, full_bleed_path) for a tier, compositing +
    caching both to disk once per (tier, canvas size) — lazily, on first use.

    This is the one piece of pre-rendering the design doc locks in; nothing
    user-specific is cached here.
    """
    slug = _slug_for_tier(tier_name)
    os.makedirs(cache_dir, exist_ok=True)
    corner_path = os.path.join(cache_dir, f"{slug}_corner.png")
    full_bleed_path = os.path.join(cache_dir, f"{slug}_bg_{canvas_size[0]}x{canvas_size[1]}.png")

    if not os.path.exists(corner_path) or not os.path.exists(full_bleed_path):
        raw_path = await fetch_ball_sprite(tier_name, cache_dir)
        await asyncio.to_thread(_render_tier_assets, raw_path, canvas_size, corner_path, full_bleed_path)

    return corner_path, full_bleed_path
