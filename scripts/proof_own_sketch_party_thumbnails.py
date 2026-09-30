"""Real-rendered proof for Own-Sketch Party Thumbnails
(Design/Own-Sketch Party Thumbnails Plan.md).

Calls the actual production `profile_card_render.render_profile_panel()`
directly (unmodified) with a mixed `PartyCardSlot` list built to exercise the
locked acceptance criteria in a single card:

  - Slot 1: a real matching sketch (1080x581 WIDE aspect, one of the design
    doc's own real-production examples) -> letterboxed, not stretched.
  - Slot 2: a real matching sketch (810x1080 TALL aspect, the other real
    example) -> letterboxed the other direction.
  - Slot 3: a SHINY mon with NO matching shiny submission (sketch_path=None)
    -> falls back to the official sprite for this slot only.
  - Slot 4: a corrupt/undecodable cached sketch file (sketch_path set but the
    file isn't a real image) -> per-slot fallback to the sprite, proving a
    load failure never breaks the tile or the rest of the party.
  - Slot 5: a plain slot with the toggle's sketch source absent entirely
    (sketch_path=None), same as any non-opted-in-style slot -> sprite only.
  - Slot 6: empty (no mon in this active party slot).

No Discord/network I/O -- sketch/sprite images are locally-generated
placeholder PNGs (simple, distinct shapes so sketch vs. sprite tiles are
visually distinguishable in the output), fed through the real render
pipeline exactly as `cogs/profile.py` would after resolving local file paths.

Run: `uv run python scripts/proof_own_sketch_party_thumbnails.py`
"""

from __future__ import annotations

import os
import sys
import tempfile

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pokesketch.profile_card_render import (  # noqa: E402
    PartyCardSlot,
    ProfileCardData,
    ServerCardStats,
    render_profile_panel,
)

OUT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "assets", "user_profile_card", "own_sketch_party_thumbnails_proof.png"
)


def _make_sketch(path: str, w: int, h: int, color: tuple[int, int, int]) -> None:
    """A simple, recognizably hand-drawn-looking placeholder sketch image at
    a REAL production aspect ratio (see design doc's own-sketch cost
    research: 1080x581 wide / 810x1080 tall are real measured examples)."""
    img = Image.new("RGBA", (w, h), (245, 245, 240, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse((w * 0.15, h * 0.15, w * 0.85, h * 0.85), outline=color, width=max(4, w // 60))
    draw.line((w * 0.1, h * 0.5, w * 0.9, h * 0.5), fill=color, width=max(3, w // 90))
    img.save(path, format="PNG")


def _make_sprite(path: str, color: tuple[int, int, int]) -> None:
    """A simple square placeholder standing in for a fetched official
    PokeAPI sprite (roughly square, unlike the sketch aspect ratios above)."""
    img = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((8, 8, 88, 88), radius=16, fill=color)
    img.save(path, format="PNG")


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    wide_sketch_path = os.path.join(out_dir, "sketch_wide.png")
    tall_sketch_path = os.path.join(out_dir, "sketch_tall.png")
    corrupt_sketch_path = os.path.join(out_dir, "sketch_corrupt.png")
    sprite_path = os.path.join(out_dir, "sprite_placeholder.png")
    shiny_sprite_path = os.path.join(out_dir, "sprite_placeholder_shiny.png")

    _make_sketch(wide_sketch_path, 1080, 581, (30, 110, 200))  # real wide example
    _make_sketch(tall_sketch_path, 810, 1080, (200, 70, 30))  # real tall example
    with open(corrupt_sketch_path, "wb") as fh:
        fh.write(b"not-a-real-png-file")
    _make_sprite(sprite_path, (90, 90, 100))
    _make_sprite(shiny_sprite_path, (240, 200, 60))

    party = [
        PartyCardSlot(
            slot=1, dex_no=1, species_name="Bulbasaur", is_shiny=False, nickname="Sprout",
            sprite_path=sprite_path, sketch_path=wide_sketch_path,
            mon_level=12, mon_exp=900,
        ),
        PartyCardSlot(
            slot=2, dex_no=4, species_name="Charmander", is_shiny=False, nickname="Blaze",
            sprite_path=sprite_path, sketch_path=tall_sketch_path,
            mon_level=8, mon_exp=400,
        ),
        # Shiny mon, no matching shiny submission -> sprite fallback.
        PartyCardSlot(
            slot=3, dex_no=25, species_name="Pikachu", is_shiny=True,
            sprite_path=shiny_sprite_path, sketch_path=None,
            mon_level=15, mon_exp=1400,
        ),
        # Cached sketch file exists but is corrupt/undecodable -> per-slot
        # fallback to sprite, never a broken tile, never all-or-nothing.
        PartyCardSlot(
            slot=4, dex_no=7, species_name="Squirtle", is_shiny=False,
            sprite_path=sprite_path, sketch_path=corrupt_sketch_path,
            mon_level=5, mon_exp=120,
        ),
        # Plain slot with no sketch source at all (toggle off / never
        # matched) -> ordinary sprite tile, same as today's default.
        PartyCardSlot(
            slot=5, dex_no=133, species_name="Eevee", is_shiny=False,
            sprite_path=sprite_path, sketch_path=None,
            mon_level=3, mon_exp=40,
        ),
        # Slot 6 intentionally omitted -> renders as the standard "Empty" tile.
    ]

    data = ProfileCardData(
        username="SketchPartyTrainer",
        level=12,
        rank_tier="Ultra Ball",
        title_badge="Ultra Ball Trainer",
        exp_current=340,
        exp_needed=500,
        global_streak=9,
        global_streak_best=21,
        global_sketch_count=142,
        accuracy_pct=87.5,
        dex_scanned=64,
        dex_total=1025,
        shiny_count=3,
        shiny_example="Pikachu",
        kudos_count=27,
        party=party,
        server=ServerCardStats(level=7, streak=4, streak_best=10, sketch_count=58),
    )

    png_bytes = render_profile_panel(data)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "render_profile_panel did not return valid PNG bytes"

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "wb") as fh:
        fh.write(png_bytes)

    img = Image.open(OUT_PATH)
    print(f"Saved proof PNG: {os.path.abspath(OUT_PATH)} ({img.width}x{img.height})")
    print("Slots: 1=wide sketch (letterboxed), 2=tall sketch (letterboxed), 3=shiny w/ no match->sprite,")
    print("       4=corrupt cached sketch->sprite fallback, 5=plain sprite-only, 6=empty")


if __name__ == "__main__":
    scratch = tempfile.mkdtemp()
    main(scratch)
