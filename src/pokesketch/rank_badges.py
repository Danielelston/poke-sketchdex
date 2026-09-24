"""Poké Ball rank badge tiers: a pure cosmetic derivation off `GlobalUser.level`.

Deliberately not the same table/concept as `GymBadge` (event trophies, guild-
scoped, already shipped) — see that model's own docstring for why these must
never share a table.

Also owns fetching + caching the ball item art from the public PokeAPI/sprites
GitHub repo, and pre-rendering the one per-tier card asset the native-embed
rebuild still needs: a small opaque corner emblem, used as the merged
`/profile` embed's `author.icon_url` (see cogs/profile.py). The flat-PNG
card's full-bleed alpha-faded background watermark (a Pillow-compositing-only
concept with no equivalent in a native `discord.Embed`, which has no
arbitrary background layer) was dropped when the profile command was rebuilt
as a native embed + buttons — see the design doc's "Visual direction update"
section for why.
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

# (level threshold, display name, PokeAPI/sprites item slug, accent RGB), ascending.
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
#
# Accent color is each ball's real dominant color (not derived from the tiny
# sprite pixels, which are too small/noisy to sample reliably) — used as the
# merged /profile embed's `color` strip so the embed still feels tier-branded.
RANK_TIERS: list[tuple[int, str, str, tuple[int, int, int]]] = [
    (1, "Poké Ball", "poke-ball", (224, 60, 55)),
    (5, "Great Ball", "great-ball", (52, 120, 199)),
    (12, "Ultra Ball", "ultra-ball", (232, 178, 43)),
    (20, "Premier Ball", "premier-ball", (219, 219, 219)),
    (30, "Luxury Ball", "luxury-ball", (212, 175, 55)),
    (45, "Master Ball", "master-ball", (168, 92, 204)),
]


def rank_for_level(level: int) -> str:
    """Highest tier whose threshold level is <= `level` (levels below the
    first threshold, including non-positive levels, get the base tier)."""
    name = RANK_TIERS[0][1]
    for threshold, tier_name, _slug, _accent in RANK_TIERS:
        if level >= threshold:
            name = tier_name
        else:
            break
    return name


def accent_color_for_tier(tier_name: str) -> tuple[int, int, int]:
    """Each tier's real ball color, used as the merged /profile embed's
    `color` strip so the embed reads as tier-branded."""
    for _threshold, name, _slug, accent in RANK_TIERS:
        if name == tier_name:
            return accent
    raise ValueError(f"Unknown rank tier: {tier_name!r}")


def _slug_for_tier(tier_name: str) -> str:
    for _threshold, name, slug, _accent in RANK_TIERS:
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


def _render_corner_emblem(raw_path: str, corner_path: str) -> None:
    """Composite the small opaque corner-emblem PNG from the raw ball sprite.

    Synchronous — run via asyncio.to_thread by the caller, same posture as
    pokebox.py's _normalize_and_save (Pillow calls block, so keep them off
    the event loop). Uses the sprite's own painted colors (crisp enough at
    64px) — unlike the retired full-bleed watermark, this crop never gets
    blown up large enough for the raw sprite's ~30x30px source detail to
    matter.
    """
    with Image.open(raw_path) as raw:
        raw = raw.convert("RGBA")
        corner = Image.new("RGBA", (CORNER_EMBLEM_SIZE, CORNER_EMBLEM_SIZE), (0, 0, 0, 0))
        thumb = raw.copy()
        thumb.thumbnail((CORNER_EMBLEM_SIZE, CORNER_EMBLEM_SIZE), Image.Resampling.LANCZOS)
        offset = ((CORNER_EMBLEM_SIZE - thumb.width) // 2, (CORNER_EMBLEM_SIZE - thumb.height) // 2)
        corner.alpha_composite(thumb, offset)
        corner.save(corner_path, format="PNG")


async def get_tier_emblem(tier_name: str, cache_dir: str) -> str:
    """Return the corner-emblem PNG path for a tier, compositing + caching it
    to disk once per tier — lazily, on first use. The one piece of
    pre-rendering the design doc still locks in for the native-embed rebuild
    (see module docstring for why the full-bleed background variant is gone).
    """
    slug = _slug_for_tier(tier_name)
    os.makedirs(cache_dir, exist_ok=True)
    corner_path = os.path.join(cache_dir, f"{slug}_corner.png")

    if not os.path.exists(corner_path):
        raw_path = await fetch_ball_sprite(tier_name, cache_dir)
        await asyncio.to_thread(_render_corner_emblem, raw_path, corner_path)

    return corner_path
