"""Player profile card rendering — the Pillow-rendered image that carries the
data density the merged `/profile` embed (cogs/profile.py) used to spread
across `embed.add_field(...)` calls.

`render_profile_panel()` is the one entry point: a pure, synchronous function
callable/testable without a live Discord interaction or any DB access — the
caller (cogs/profile.py's `_build_card_data()`) converts already-fetched DB
rows into a `ProfileCardData` before calling this. No network I/O — party
sprites are fetched by the async caller beforehand (see
pokeapi.PokeApiClient.get_sprite_image_path) and only a resolved local file
path is passed in; this module just opens that file synchronously via
`Image.open()` — so this stays fully deterministic and unit-testable in
isolation, given system DejaVu fonts and whatever sprite files already exist
on disk.

Layout intent (see assets/user_profile_card/code.html for the mockup this
follows, and the module-level color tokens below, which are the LOCKED design
tokens for THIS render — not the raw Discord-Activity-UI palette dump in
assets/user_profile_card/DESIGN.md): an identity/header area (with a
chip-styled title badge), a gradient EXP bar (accent-color-driven when the
target has a Discord accent color, gold-to-green otherwise), a mobile-
friendly 2x3 grid of stat tiles (Global / This Server / Streak / Pokédex /
Shiny / Kudos), and a mobile-friendly 3x2 grid of party tiles (always 6
slots, empty ones rendered as placeholders) with per-mon borders (gold for
shiny, blurple otherwise) and an official-artwork sprite. The card is taller
than it is wide (CARD_WIDTH=560) so it reads well on a phone screen without
horizontal scrolling. Deliberately does NOT reproduce the mockup's full-bleed
watermark background (see rank_badges.py's module docstring for why that was
dropped). Each party tile shows a "Lv. {N}" badge (PartyCardSlot.mon_level, same
_draw_level_badge() style as the header) plus a progress readout underneath
("x/y EXP" toward the next level, or "MAX" at leveling.MON_LEVEL_CAP) — see
the Party Mon Leveling Plan design doc.
"""

from __future__ import annotations

import colorsys
import io
import logging
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

from . import leveling

log = logging.getLogger(__name__)

# --- Locked color tokens (exact hex from the design doc's Discord-native
# palette, not the raw Discord-Activity-UI dump in DESIGN.md) -------------
COLOR_SURFACE = (0x2B, 0x2D, 0x31)  # panel/tile background
COLOR_BASE = (0x1E, 0x1F, 0x22)  # canvas background
COLOR_BLURPLE = (0x58, 0x65, 0xF2)
COLOR_GOLD = (0xF0, 0xB2, 0x32)  # warning gold / EXP
COLOR_GREEN = (0x23, 0xA5, 0x5A)  # HP / success green
COLOR_TEXT = (0xDB, 0xDE, 0xE1)  # on-surface text
COLOR_SUBTEXT = (0x94, 0x9B, 0xA4)  # subtext
COLOR_MUTED_BORDER = (0x3F, 0x41, 0x47)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

CARD_WIDTH = 560
MARGIN = 24
PANEL_RADIUS = 14

# --- Header -----------------------------------------------------------
HEADER_HEIGHT = 104
HEADER_FONT_SIZE = 30
HEADER_SUB_FONT_SIZE = 20
HEADER_AVATAR_SIZE = 64  # circular, left-aligned within the header panel
HEADER_AVATAR_MARGIN = 16  # gap from the header panel's left/top edge
HEADER_USERNAME_SUB_GAP = 46  # vertical gap from username baseline to the level badge/chip sub-line
HEADER_AVATAR_RING_WIDTH = 3  # thickness of the avatar's border ring

# --- Header title-badge chip (MUI-chip-style pill) ----------------------
CHIP_PAD_X = 11
CHIP_PAD_Y = 5

# --- EXP bar ------------------------------------------------------------
EXP_GAP_ABOVE = 16
EXP_LABEL_HEIGHT = 22
EXP_BAR_HEIGHT = 18
EXP_BAR_RADIUS = 9
EXP_SECTION_HEIGHT = EXP_GAP_ABOVE + EXP_LABEL_HEIGHT + EXP_BAR_HEIGHT

# --- Stat tile grid: mobile-friendly 2 columns x 3 rows ------------------
STAT_GAP_ABOVE = 20
STAT_COLS = 2
STAT_ROWS = 3
STAT_TILE_GAP = 12
STAT_TILE_HEIGHT = 92
STAT_TILE_WIDTH = (CARD_WIDTH - MARGIN * 2 - STAT_TILE_GAP * (STAT_COLS - 1)) // STAT_COLS
STAT_GRID_HEIGHT = STAT_TILE_HEIGHT * STAT_ROWS + STAT_TILE_GAP * (STAT_ROWS - 1)
STAT_LABEL_FONT_SIZE = 14
STAT_VALUE_FONT_SIZE = 24
STAT_SUB_FONT_SIZE = 14

# --- Party grid: always 6 slots, mobile-friendly 3 columns x 2 rows -------
PARTY_GAP_ABOVE = 20
PARTY_LABEL_HEIGHT = 24
PARTY_SLOTS = 6
PARTY_COLS = 3
PARTY_ROWS = 2
PARTY_TILE_GAP = 12
PARTY_TILE_WIDTH = (CARD_WIDTH - MARGIN * 2 - PARTY_TILE_GAP * (PARTY_COLS - 1)) // PARTY_COLS
PARTY_NAME_FONT_SIZE = 14
PARTY_DEX_FONT_SIZE = 12
PARTY_SPRITE_SIZE = 44
PARTY_SLOT_BADGE_SIZE = 26  # corner-ribbon badge, not a circle — see _draw_corner_ribbon_badge
PARTY_TILE_RADIUS = 10  # must match the radius passed to _draw_panel() for party tiles
PARTY_LEVEL_BADGE_PAD_X = 8  # smaller than the header's level badge (party tiles are narrow)
PARTY_LEVEL_BADGE_PAD_Y = 4
PARTY_LEVEL_FONT_SIZE = 11

# EXP progress readout row — a compact "x/y EXP" (or "MAX" at the level cap)
# drawn under the name/level row. See Party Mon Leveling Plan design doc.
PARTY_EXP_FONT_SIZE = 12
PARTY_EXP_LINE_HEIGHT = 16
PARTY_EXP_LINE_GAP = 2

# Image/sketch-canvas area: reserves the MOST vertical room in the tile since
# this is where a future "user sketches here" canvas will live (sprite is a
# stand-in for now) — per user direction (2026-09-24): "Let the sprite
# section hold the most space because we plan to let the users sketch in the
# future here instead of the sprite."
PARTY_IMAGE_MARGIN = 8  # inset from the tile's own top/left/right edges
PARTY_IMAGE_HEIGHT = 90
PARTY_IMAGE_RADIUS = 8
PARTY_IMAGE_GAP_BELOW = 8
PARTY_NAME_LINE_HEIGHT = 22  # nickname (or species fallback) + Lv. badge, same row
PARTY_TEXT_LINE_GAP = 2
PARTY_SPECIES_LINE_HEIGHT = 16  # "{species} #{dex}" — only drawn when a nickname is set
PARTY_BOTTOM_PAD = 8
PARTY_TILE_HEIGHT = (
    PARTY_IMAGE_MARGIN
    + PARTY_IMAGE_HEIGHT
    + PARTY_IMAGE_GAP_BELOW
    + PARTY_NAME_LINE_HEIGHT
    + PARTY_TEXT_LINE_GAP
    + PARTY_SPECIES_LINE_HEIGHT
    + PARTY_TEXT_LINE_GAP
    + PARTY_EXP_LINE_HEIGHT
    + PARTY_BOTTOM_PAD
)
PARTY_SECTION_HEIGHT = (
    PARTY_LABEL_HEIGHT + PARTY_TILE_HEIGHT * PARTY_ROWS + PARTY_TILE_GAP * (PARTY_ROWS - 1)
)

CARD_HEIGHT = (
    MARGIN
    + HEADER_HEIGHT
    + EXP_SECTION_HEIGHT
    + STAT_GAP_ABOVE
    + STAT_GRID_HEIGHT
    + PARTY_GAP_ABOVE
    + PARTY_SECTION_HEIGHT
    + MARGIN
)


@dataclass
class ServerCardStats:
    level: int
    streak: int
    streak_best: int
    sketch_count: int


@dataclass
class PartyCardSlot:
    slot: int
    dex_no: int
    # Species display name (e.g. "Lucario") — always present, unlike nickname.
    species_name: str
    is_shiny: bool
    # User-set cosmetic nickname (CaughtMon.nickname), separate from
    # species_name so the render can show BOTH per the mock's two-line
    # layout ("AuraKnight" / "Lucario #0448") instead of collapsing to one.
    nickname: str | None = None
    # Local file path to a cached official-artwork PNG (fetched by the async
    # caller via pokeapi.PokeApiClient.get_sprite_image_path — this render
    # module stays synchronous and just opens+composites it), or None if no
    # sprite was fetched/available.
    sprite_path: str | None = None
    # CaughtMon.mon_level — the mon's current level (party mon leveling
    # feature, see Party Mon Leveling Plan design doc).
    mon_level: int = 1
    # CaughtMon.mon_exp — raw cumulative EXP backing mon_level. Drives the
    # tile's progress readout (x/y EXP, or MAX at leveling.MON_LEVEL_CAP).
    mon_exp: int = 0


@dataclass
class ProfileCardData:
    username: str
    level: int
    rank_tier: str
    title_badge: str
    exp_current: int
    exp_needed: int
    global_streak: int
    global_streak_best: int
    global_sketch_count: int
    accuracy_pct: float
    dex_scanned: int
    dex_total: int
    shiny_count: int
    shiny_example: str | None
    kudos_count: int
    party: list[PartyCardSlot] = field(default_factory=list)
    server: ServerCardStats | None = None
    # The target's Discord profile accent color (discord.User.accent_color,
    # only populated on a freshly-fetched User, not a cached Member/User) —
    # drives the EXP bar's gradient when present; None (most users never set
    # one) falls back to the locked gold->green pair.
    accent_color: tuple[int, int, int] | None = None
    # Local file path to a cached copy of the target's Discord avatar
    # (fetched by the async caller — see cogs/profile.py — since this
    # render module stays synchronous; same pattern as PartyCardSlot's
    # sprite_path). None renders a plain initial-letter placeholder circle
    # instead of leaving a blank gap.
    avatar_path: str | None = None


def _truncate_to_width(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Truncate `text` with a trailing ellipsis so its rendered width (via
    this exact font) fits within `max_width` — measures real glyph widths via
    `font.getlength()` rather than a fixed character-count budget, which can
    still overrun the canvas for bold display fonts (same technique/reason as
    the deleted cards.py module's `_truncate_to_width`)."""
    if font.getlength(text) <= max_width:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.getlength(text[:mid] + ellipsis) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis if lo > 0 else ellipsis


def _draw_circular_avatar(
    card: Image.Image,
    center_x: int,
    center_y: int,
    size: int,
    avatar_path: str | None,
    fallback_letter: str,
    border_color: tuple[int, int, int],
) -> None:
    """Composite a circular avatar (Discord-style) centered at (center_x,
    center_y). Opens+resizes+circle-masks the cached image at `avatar_path`
    (same synchronous-open, async-pre-fetch pattern as party tile sprites —
    see PartyCardSlot's sprite_path / _draw_party_tile). Falls back to a
    plain filled circle with the target's first initial when no avatar was
    fetched/available, rather than leaving a blank gap, same posture as the
    party grid's "Empty" placeholder tiles.

    The border ring sits OUTSIDE the avatar image, not overlapping it: the
    avatar disk is sized to `size - 2*ring_width` so its edge lands exactly
    at the ring's inner edge, and the ring itself is drawn at the full
    `size` bounding box — no bleed past the ring, no gap between them.
    Everything is supersampled 4x then downscaled for anti-aliased edges
    (a straight `ImageDraw.ellipse` at avatar-badge sizes is visibly
    jagged/pixelated, as flagged against a reference screenshot)."""
    x0, y0 = center_x - size // 2, center_y - size // 2
    ring_w = HEADER_AVATAR_RING_WIDTH
    ss = 4  # supersample factor for smooth anti-aliased circles
    ss_size = size * ss
    avatar_diameter = size - 2 * ring_w
    ss_avatar_diameter = avatar_diameter * ss
    avatar_offset = ring_w * ss  # avatar sits inset by exactly one ring width

    composite = Image.new("RGBA", (ss_size, ss_size), (0, 0, 0, 0))

    avatar_mask = Image.new("L", (ss_avatar_diameter, ss_avatar_diameter), 0)
    ImageDraw.Draw(avatar_mask).ellipse((0, 0, ss_avatar_diameter - 1, ss_avatar_diameter - 1), fill=255)

    if avatar_path:
        try:
            with Image.open(avatar_path) as avatar_img:
                avatar_img = avatar_img.convert("RGBA").resize(
                    (ss_avatar_diameter, ss_avatar_diameter), Image.Resampling.LANCZOS
                )
                composite.paste(avatar_img, (avatar_offset, avatar_offset), avatar_mask)
        except Exception:
            log.warning("Failed to composite avatar from %r", avatar_path, exc_info=True)
            avatar_path = None  # fall through to the placeholder below

    if not avatar_path:
        circle = Image.new("RGBA", (ss_avatar_diameter, ss_avatar_diameter), (0, 0, 0, 0))
        cd = ImageDraw.Draw(circle)
        cd.ellipse((0, 0, ss_avatar_diameter - 1, ss_avatar_diameter - 1), fill=COLOR_MUTED_BORDER)
        letter = (fallback_letter or "?")[0].upper()
        letter_font = ImageFont.truetype(FONT_BOLD, int(ss_avatar_diameter * 0.45))
        bbox = cd.textbbox((0, 0), letter, font=letter_font)
        cd.text(
            (
                ss_avatar_diameter / 2 - (bbox[2] - bbox[0]) / 2 - bbox[0],
                ss_avatar_diameter / 2 - (bbox[3] - bbox[1]) / 2 - bbox[1],
            ),
            letter, font=letter_font, fill=COLOR_TEXT,
        )
        composite.paste(circle, (avatar_offset, avatar_offset), circle)

    # Ring drawn at the FULL outer bounding box (not inset) so it sits
    # outside the avatar disk rather than overlapping its edge pixels —
    # its inner edge lands exactly on the avatar's own edge (both derived
    # from the same ring_w), so there's no bleed and no visible gap.
    ss_ring_w = ring_w * ss
    ring_inset = ss_ring_w // 2
    ImageDraw.Draw(composite).ellipse(
        (ring_inset, ring_inset, ss_size - 1 - ring_inset, ss_size - 1 - ring_inset),
        outline=border_color,
        width=ss_ring_w,
    )

    composite = composite.resize((size, size), Image.Resampling.LANCZOS)
    card.paste(composite, (x0, y0), composite)


def _draw_level_badge(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    pad_x: int = 12,
    pad_y: int = 6,
    radius: int = 8,
) -> int:
    """Draw a small rounded-rect "Lv. {N}" badge — visually distinct from
    the pill-shaped title-badge chip (_draw_chip): a modest corner radius
    (not a full pill), a dark fill + subtle border, bold text. Matches the
    user-supplied reference image. Returns the pixel width consumed."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    badge_w = int(text_w + pad_x * 2)
    badge_h = int(text_h + pad_y * 2)
    draw.rounded_rectangle(
        (x, y, x + badge_w, y + badge_h), radius=radius, fill=COLOR_BASE, outline=COLOR_MUTED_BORDER, width=1
    )
    draw.text((x + pad_x - bbox[0], y + pad_y - bbox[1]), text, font=font, fill=COLOR_TEXT)
    return badge_w


def _draw_chip(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    bg_color: tuple[int, int, int],
    text_color: tuple[int, int, int],
) -> int:
    """Draw a rounded-pill "chip" (MUI-chip style): a small rounded-rect
    background behind `text`, top-left anchored at (x, y). Returns the pixel
    width consumed so a caller could lay out multiple chips in a row.
    Measures via `draw.textbbox()` (not a fixed size) so it degrades safely
    on an empty string (still draws a minimal pill) or a very long string
    (grows to fit — callers that need a width cap truncate the text first via
    `_truncate_to_width`, same as this file's other text-measuring call
    sites)."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    chip_w = text_w + CHIP_PAD_X * 2
    chip_h = text_h + CHIP_PAD_Y * 2
    draw.rounded_rectangle((x, y, x + chip_w, y + chip_h), radius=chip_h // 2, fill=bg_color)
    draw.text((x + CHIP_PAD_X - bbox[0], y + CHIP_PAD_Y - bbox[1]), text, font=font, fill=text_color)
    return chip_w


def _gradient_from_accent(accent: tuple[int, int, int]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Derive a two-tone (lighter, darker) gradient pair from a single accent
    color so the EXP bar keeps its "gradient bar" visual identity instead of
    falling back to a flat single-hue fill. HSL lightness +/-15%, clamped to
    [0, 1]."""
    r, g, b = (c / 255.0 for c in accent)
    hue, lightness, sat = colorsys.rgb_to_hls(r, g, b)
    light = colorsys.hls_to_rgb(hue, min(1.0, lightness + 0.15), sat)
    dark = colorsys.hls_to_rgb(hue, max(0.0, lightness - 0.15), sat)
    light_rgb = tuple(round(ch * 255) for ch in light)
    dark_rgb = tuple(round(ch * 255) for ch in dark)
    return light_rgb, dark_rgb


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int] = COLOR_SURFACE,
    outline: tuple[int, int, int] | None = None,
    outline_width: int = 1,
    radius: int = PANEL_RADIUS,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=outline_width)


def _draw_gradient_bar(
    card: Image.Image,
    box: tuple[int, int, int, int],
    frac: float,
    start_color: tuple[int, int, int] = COLOR_GOLD,
    end_color: tuple[int, int, int] = COLOR_GREEN,
) -> None:
    """Draw a rounded-rect EXP bar: a dim track, filled left-to-right with a
    horizontal `start_color`-to-`end_color` gradient (the locked gold-to-
    green pair by default; the caller passes an accent-derived two-tone pair
    instead when the target has a Discord accent color), composited through
    a rounded-rect alpha mask so a partially-filled bar still has rounded
    ends (built as a small gradient strip rather than a plain flat fill, per
    the locked design spec)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(box, radius=EXP_BAR_RADIUS, fill=COLOR_MUTED_BORDER)

    frac = 0.0 if frac < 0 else min(1.0, frac)
    fill_w = int(w * frac)
    if fill_w <= 0:
        return

    gradient = Image.new("RGB", (fill_w, h))
    grad_pixels = gradient.load()
    for gx in range(fill_w):
        t = gx / max(1, w - 1)  # gradient position relative to the FULL bar width
        r = int(start_color[0] + (end_color[0] - start_color[0]) * t)
        g = int(start_color[1] + (end_color[1] - start_color[1]) * t)
        b = int(start_color[2] + (end_color[2] - start_color[2]) * t)
        for gy in range(h):
            grad_pixels[gx, gy] = (r, g, b)

    mask = Image.new("L", (fill_w, h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, fill_w - 1, h - 1), radius=EXP_BAR_RADIUS, fill=255)

    card.paste(gradient, (x0, y0), mask)


def _draw_stat_tile(
    card: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    subtext: str | None,
    label_font: ImageFont.FreeTypeFont,
    value_font: ImageFont.FreeTypeFont,
    sub_font: ImageFont.FreeTypeFont,
) -> None:
    x0, y0, x1, y1 = box
    _draw_panel(draw, box, fill=COLOR_SURFACE, outline=COLOR_MUTED_BORDER)
    pad = 12
    tx = x0 + pad
    draw.text((tx, y0 + pad), label.upper(), font=label_font, fill=COLOR_SUBTEXT)
    max_w = (x1 - x0) - pad * 2
    value = _truncate_to_width(value, value_font, max_w)
    draw.text((tx, y0 + pad + 20), value, font=value_font, fill=COLOR_TEXT)
    if subtext:
        subtext = _truncate_to_width(subtext, sub_font, max_w)
        draw.text((tx, y0 + pad + 52), subtext, font=sub_font, fill=COLOR_SUBTEXT)


def _draw_streak_tile(
    card: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    global_streak: int,
    global_streak_best: int,
    server_streak: int | None,
    server_streak_best: int | None,
    label_font: ImageFont.FreeTypeFont,
    value_font: ImageFont.FreeTypeFont,
    sub_font: ImageFont.FreeTypeFont,
) -> None:
    """Streak tile, two internal sub-columns instead of _draw_stat_tile's
    single value/subtext stack: left = Global ("G: {n}" / "best: {best}"),
    right = This Server (same shape, or an em-dash placeholder when
    `server_streak` is None i.e. no per-server data for this guild) — kept
    as its own draw function rather than overloading `_draw_stat_tile`,
    since no other tile needs a two-column split."""
    x0, y0, x1, y1 = box
    _draw_panel(draw, box, fill=COLOR_SURFACE, outline=COLOR_MUTED_BORDER)
    pad = 12
    draw.text((x0 + pad, y0 + pad), "STREAK", font=label_font, fill=COLOR_SUBTEXT)

    col_w = ((x1 - x0) - pad * 2) // 2
    left_x = x0 + pad
    right_x = x0 + pad + col_w

    value_y = y0 + pad + 20
    sub_y = y0 + pad + 52

    def draw_column(cx: int, prefix: str, streak: int | None, best: int | None) -> None:
        max_w = col_w - 6  # small inter-column gutter so text doesn't touch the divider
        if streak is None:
            draw.text((cx, value_y), "—", font=value_font, fill=COLOR_TEXT)
            sub = _truncate_to_width("No data", sub_font, max_w)
            draw.text((cx, sub_y), sub, font=sub_font, fill=COLOR_SUBTEXT)
            return
        value_text = _truncate_to_width(f"{prefix}: {streak}", value_font, max_w)
        draw.text((cx, value_y), value_text, font=value_font, fill=COLOR_TEXT)
        sub_text = _truncate_to_width(f"best: {best}", sub_font, max_w)
        draw.text((cx, sub_y), sub_text, font=sub_font, fill=COLOR_SUBTEXT)

    draw_column(left_x, "G", global_streak, global_streak_best)
    draw_column(right_x, "S", server_streak, server_streak_best)


def _draw_corner_ribbon_badge(
    card: Image.Image,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    bg_color: tuple[int, int, int],
    text_color: tuple[int, int, int],
    size: int = PARTY_SLOT_BADGE_SIZE,
    tile_radius: int = PARTY_TILE_RADIUS,
) -> None:
    """Corner-ribbon slot badge: CSS `border-radius: 0 <tile_radius>px 0
    <tile_radius>px` on a small rectangle flush in the tile's top-right
    corner — top-right corner rounded to flush-match the tile's own outer
    corner, bottom-left corner rounded by the same radius for the ribbon's
    inward tab curve, top-left/bottom-right corners left sharp (they sit
    flush against the tile's own top/right edges)."""
    x0, y0, x1, y1 = box
    badge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bd = ImageDraw.Draw(badge)
    bd.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=tile_radius, fill=bg_color,
        corners=(False, True, False, True),  # (top-left, top-right, bottom-right, bottom-left)
    )
    px, py = x1 - size, y0
    card.paste(badge, (px, py), badge)

    # Center the digit in the badge box (a rounded rect has much more
    # interior room than the quarter-circle shape did).
    text_cx = px + size * 0.5
    text_cy = py + size * 0.5
    bbox = ImageDraw.Draw(card).textbbox((0, 0), text, font=font)
    ImageDraw.Draw(card).text(
        (text_cx - (bbox[2] - bbox[0]) / 2 - bbox[0], text_cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
        text, font=font, fill=text_color,
    )


def _draw_party_tile(
    card: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    slot: PartyCardSlot | None,
    name_font: ImageFont.FreeTypeFont,
    dex_font: ImageFont.FreeTypeFont,
    slot_badge_font: ImageFont.FreeTypeFont,
    level_badge_font: ImageFont.FreeTypeFont,
    exp_font: ImageFont.FreeTypeFont,
) -> None:
    x0, y0, x1, y1 = box
    if slot is None:
        _draw_panel(draw, box, fill=COLOR_SURFACE, outline=COLOR_MUTED_BORDER, radius=PARTY_TILE_RADIUS)
        empty_w = (x1 - x0) - 16
        label = _truncate_to_width("Empty", dex_font, empty_w)
        cx = x0 + (x1 - x0) // 2
        cy = y0 + (y1 - y0) // 2
        bbox = draw.textbbox((0, 0), label, font=dex_font)
        draw.text(
            (cx - (bbox[2] - bbox[0]) // 2, cy - (bbox[3] - bbox[1]) // 2),
            label,
            font=dex_font,
            fill=COLOR_SUBTEXT,
        )
        return

    border = COLOR_GOLD if slot.is_shiny else COLOR_BLURPLE
    _draw_panel(draw, box, fill=COLOR_SURFACE, outline=None, radius=PARTY_TILE_RADIUS)

    pad = 8
    text_left = x0 + pad
    text_right = x1 - pad
    max_w = text_right - text_left

    # Image/sketch-canvas area — holds the MOST space in the tile, since
    # this is a stand-in for a future user-drawn sketch, not just a sprite
    # thumbnail. A slightly-darker rounded panel gives it a distinct
    # "canvas" feel even before real sketch uploads exist.
    image_box = (
        x0 + PARTY_IMAGE_MARGIN, y0 + PARTY_IMAGE_MARGIN,
        x1 - PARTY_IMAGE_MARGIN, y0 + PARTY_IMAGE_MARGIN + PARTY_IMAGE_HEIGHT,
    )
    _draw_panel(draw, image_box, fill=COLOR_BASE, outline=None, radius=PARTY_IMAGE_RADIUS)
    if slot.sprite_path:
        try:
            with Image.open(slot.sprite_path) as sprite_img:
                sprite_img = sprite_img.convert("RGBA").resize(
                    (PARTY_SPRITE_SIZE, PARTY_SPRITE_SIZE), Image.Resampling.LANCZOS
                )
                sprite_cx = (image_box[0] + image_box[2]) // 2
                sprite_cy = (image_box[1] + image_box[3]) // 2
                card.paste(
                    sprite_img,
                    (sprite_cx - PARTY_SPRITE_SIZE // 2, sprite_cy - PARTY_SPRITE_SIZE // 2),
                    sprite_img,
                )
        except Exception:
            log.warning("Failed to composite party sprite from %r", slot.sprite_path, exc_info=True)

    # Row 1: nickname (falls back to species name if unset, per explicit
    # user direction 2026-09-24 — no "Unnamed" placeholder, no duplicate
    # species line when there's no nickname) on the left, "Lv. {N}" badge
    # right-aligned on the same row.
    name_row_y = image_box[3] + PARTY_IMAGE_GAP_BELOW
    level_text = f"Lv. {slot.mon_level}"
    level_bbox = draw.textbbox((0, 0), level_text, font=level_badge_font)
    level_badge_w = int((level_bbox[2] - level_bbox[0]) + PARTY_LEVEL_BADGE_PAD_X * 2)
    level_badge_h = int((level_bbox[3] - level_bbox[1]) + PARTY_LEVEL_BADGE_PAD_Y * 2)
    level_x = text_right - level_badge_w
    level_y = name_row_y + (PARTY_NAME_LINE_HEIGHT - level_badge_h) // 2
    _draw_level_badge(
        draw, level_x, level_y, level_text, level_badge_font,
        pad_x=PARTY_LEVEL_BADGE_PAD_X, pad_y=PARTY_LEVEL_BADGE_PAD_Y,
    )

    display_name = slot.nickname or slot.species_name
    name_max_w = max(0, (level_x - 6) - text_left)  # stop short of the level badge
    display_name = _truncate_to_width(display_name, name_font, name_max_w)
    name_color = COLOR_GOLD if slot.is_shiny else COLOR_TEXT
    name_bbox = draw.textbbox((0, 0), display_name, font=name_font)
    name_text_y = name_row_y + (PARTY_NAME_LINE_HEIGHT - (name_bbox[3] - name_bbox[1])) // 2 - name_bbox[1]
    draw.text((text_left, name_text_y), display_name, font=name_font, fill=name_color)

    # Row 2: always drawn now — "{species} #{dex}" when a nickname is set
    # (row 1 already shows the nickname, so this adds the species context),
    # or just "#{dex}" when there's no nickname (row 1 already shows the
    # species name, so repeating it here would be redundant). Shiny mons get
    # a leading star emoji: "★ {species} #{dex}" / "★ #{dex}" — per user
    # direction (2026-09-24): the tile border ring was dropped, so shiny
    # status is now conveyed by gold badge + gold name + this star only.
    species_row_y = name_row_y + PARTY_NAME_LINE_HEIGHT + PARTY_TEXT_LINE_GAP
    if slot.nickname:
        species_label = f"{slot.species_name} #{slot.dex_no:04d}"
    else:
        species_label = f"#{slot.dex_no:04d}"
    if slot.is_shiny:
        species_label = f"★ {species_label}"
    species_label = _truncate_to_width(species_label, dex_font, max_w)
    species_color = COLOR_GOLD if slot.is_shiny else COLOR_SUBTEXT
    draw.text((text_left, species_row_y), species_label, font=dex_font, fill=species_color)

    # Row 3: EXP progress readout — "x/y EXP" toward the next level, or
    # "MAX" (not a division) once the mon has reached leveling.MON_LEVEL_CAP.
    exp_row_y = species_row_y + PARTY_SPECIES_LINE_HEIGHT + PARTY_TEXT_LINE_GAP
    if slot.mon_level >= leveling.MON_LEVEL_CAP:
        exp_label = "MAX"
    else:
        _lvl, into, need = leveling.mon_exp_into_level(slot.mon_exp)
        exp_label = f"{into}/{need} EXP"
    exp_label = _truncate_to_width(exp_label, exp_font, max_w)
    draw.text((text_left, exp_row_y), exp_label, font=exp_font, fill=COLOR_SUBTEXT)

    # Slot number as a small corner-ribbon badge (top-right) instead of a
    # plain circle — flush with the tile's own rounded corner, concave inner
    # arc, per the mockup reference the user supplied. Bold font for
    # legibility.
    badge_text_color = COLOR_BASE if slot.is_shiny else COLOR_TEXT
    _draw_corner_ribbon_badge(
        card, box, str(slot.slot), slot_badge_font, bg_color=border, text_color=badge_text_color
    )


def render_profile_panel(data: ProfileCardData) -> bytes:
    """Composite the stat-grid + EXP-bar + party-grid profile card and
    return raw PNG bytes. Pure/synchronous/deterministic — no network I/O
    (party sprites are pre-fetched to local disk paths by the caller; see
    module docstring)."""
    card = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), COLOR_BASE)
    draw = ImageDraw.Draw(card)

    header_font = ImageFont.truetype(FONT_BOLD, HEADER_FONT_SIZE)
    sub_font = ImageFont.truetype(FONT_REGULAR, HEADER_SUB_FONT_SIZE)
    exp_label_font = ImageFont.truetype(FONT_REGULAR, 15)
    stat_label_font = ImageFont.truetype(FONT_BOLD, STAT_LABEL_FONT_SIZE)
    stat_value_font = ImageFont.truetype(FONT_BOLD, STAT_VALUE_FONT_SIZE)
    stat_sub_font = ImageFont.truetype(FONT_REGULAR, STAT_SUB_FONT_SIZE)
    party_name_font = ImageFont.truetype(FONT_BOLD, PARTY_NAME_FONT_SIZE)
    party_dex_font = ImageFont.truetype(FONT_REGULAR, PARTY_DEX_FONT_SIZE)
    party_slot_badge_font = ImageFont.truetype(FONT_BOLD, PARTY_DEX_FONT_SIZE)
    party_level_badge_font = ImageFont.truetype(FONT_BOLD, PARTY_LEVEL_FONT_SIZE)
    party_exp_font = ImageFont.truetype(FONT_REGULAR, PARTY_EXP_FONT_SIZE)
    section_label_font = ImageFont.truetype(FONT_BOLD, 15)

    # --- Header / identity area ------------------------------------
    header_box = (MARGIN, MARGIN, CARD_WIDTH - MARGIN, MARGIN + HEADER_HEIGHT)
    _draw_panel(draw, header_box, fill=COLOR_SURFACE, outline=COLOR_MUTED_BORDER)

    # Avatar, LEFT-aligned within the header panel — username/level/title
    # text is laid out to its right instead of stopping short of a
    # right-aligned avatar.
    avatar_border = (
        _gradient_from_accent(data.accent_color)[0] if data.accent_color is not None else COLOR_BLURPLE
    )
    avatar_cx = MARGIN + HEADER_AVATAR_MARGIN + HEADER_AVATAR_SIZE // 2
    avatar_cy = MARGIN + HEADER_HEIGHT // 2
    _draw_circular_avatar(
        card, avatar_cx, avatar_cy, HEADER_AVATAR_SIZE, data.avatar_path, data.username, avatar_border
    )
    draw = ImageDraw.Draw(card)  # re-bind after paste() mutated the underlying image

    tx = avatar_cx + HEADER_AVATAR_SIZE // 2 + 16
    ty = MARGIN + 14
    text_right_limit = CARD_WIDTH - MARGIN - 16
    username = _truncate_to_width(data.username, header_font, text_right_limit - tx)
    draw.text((tx, ty), username, font=header_font, fill=COLOR_TEXT)

    # Sub-line: "[Lv. {N}] [chip: {title_badge}]" — level now its own small
    # rounded badge (not plain "Level {N} ·" text) with extra vertical
    # margin under the username, per the user-supplied reference image.
    sub_y = ty + HEADER_USERNAME_SUB_GAP
    level_text = f"Lv. {data.level}"
    level_badge_w = _draw_level_badge(draw, tx, sub_y, level_text, sub_font)
    chip_x = tx + level_badge_w + 8
    chip_right_limit = text_right_limit
    chip_max_text_w = max(0, (chip_right_limit - chip_x) - CHIP_PAD_X * 2)
    badge_text = _truncate_to_width(data.title_badge, sub_font, chip_max_text_w)
    _draw_chip(draw, chip_x, sub_y, badge_text, sub_font, COLOR_BLURPLE, COLOR_TEXT)

    # --- EXP bar -----------------------------------------------------
    exp_y = MARGIN + HEADER_HEIGHT + EXP_GAP_ABOVE
    label_text = f"{data.exp_current} / {data.exp_needed} EXP"
    draw.text((MARGIN, exp_y), label_text, font=exp_label_font, fill=COLOR_SUBTEXT)
    bar_y0 = exp_y + EXP_LABEL_HEIGHT
    bar_box = (MARGIN, bar_y0, CARD_WIDTH - MARGIN, bar_y0 + EXP_BAR_HEIGHT)
    frac = 0.0 if data.exp_needed <= 0 else data.exp_current / data.exp_needed
    if data.accent_color is not None:
        bar_start, bar_end = _gradient_from_accent(data.accent_color)
    else:
        bar_start, bar_end = COLOR_GOLD, COLOR_GREEN
    _draw_gradient_bar(card, bar_box, frac, bar_start, bar_end)
    draw = ImageDraw.Draw(card)  # re-bind after paste() mutated the underlying image

    # --- Stat tile grid: Global / This Server / Streak / Pokédex / Shiny / Kudos
    grid_y = bar_y0 + EXP_BAR_HEIGHT + STAT_GAP_ABOVE

    def tile_box(col: int, row: int) -> tuple[int, int, int, int]:
        x0 = MARGIN + col * (STAT_TILE_WIDTH + STAT_TILE_GAP)
        y0 = grid_y + row * (STAT_TILE_HEIGHT + STAT_TILE_GAP)
        return (x0, y0, x0 + STAT_TILE_WIDTH, y0 + STAT_TILE_HEIGHT)

    _draw_stat_tile(
        card, draw, tile_box(0, 0), "Global",
        f"Lv {data.level}",
        f"{data.global_sketch_count} sketches total",
        stat_label_font, stat_value_font, stat_sub_font,
    )
    if data.server is not None:
        _draw_stat_tile(
            card, draw, tile_box(1, 0), "This Server",
            f"Lv {data.server.level}",
            f"{data.server.sketch_count} sketches here",
            stat_label_font, stat_value_font, stat_sub_font,
        )
    else:
        _draw_stat_tile(
            card, draw, tile_box(1, 0), "This Server",
            "—",
            "No per-server data yet",
            stat_label_font, stat_value_font, stat_sub_font,
        )

    # Combined Streak tile (frees up the third slot from the dropped
    # Accuracy tile): two internal sub-columns (Global left, This Server
    # right), each with its own "G: n" / "best: n" pair — see
    # _draw_streak_tile's docstring for why this tile needs its own draw
    # function instead of reusing _draw_stat_tile's single-column layout.
    _draw_streak_tile(
        card, draw, tile_box(0, 1),
        data.global_streak, data.global_streak_best,
        data.server.streak if data.server is not None else None,
        data.server.streak_best if data.server is not None else None,
        stat_label_font, stat_value_font, stat_sub_font,
    )

    dex_pct = (data.dex_scanned / data.dex_total * 100) if data.dex_total else 0.0
    _draw_stat_tile(
        card, draw, tile_box(1, 1), "Pokédex",
        f"{data.dex_scanned}/{data.dex_total}",
        f"{dex_pct:.1f}% complete",
        stat_label_font, stat_value_font, stat_sub_font,
    )
    shiny_sub = f"Latest: {data.shiny_example}" if data.shiny_example else None
    _draw_stat_tile(
        card, draw, tile_box(0, 2), "Shiny",
        f"{data.shiny_count} species ★",
        shiny_sub,
        stat_label_font, stat_value_font, stat_sub_font,
    )
    _draw_stat_tile(
        card, draw, tile_box(1, 2), "Kudos",
        str(data.kudos_count),
        None,
        stat_label_font, stat_value_font, stat_sub_font,
    )

    # --- Party grid: always 6 slots, 3 columns x 2 rows ----------------
    party_label_y = grid_y + STAT_GRID_HEIGHT + PARTY_GAP_ABOVE
    draw.text((MARGIN, party_label_y), "ACTIVE PARTY", font=section_label_font, fill=COLOR_SUBTEXT)
    party_y0 = party_label_y + PARTY_LABEL_HEIGHT
    slots_in_order = data.party[:PARTY_SLOTS]
    for i in range(PARTY_SLOTS):
        col, row = i % PARTY_COLS, i // PARTY_COLS
        x0 = MARGIN + col * (PARTY_TILE_WIDTH + PARTY_TILE_GAP)
        y0 = party_y0 + row * (PARTY_TILE_HEIGHT + PARTY_TILE_GAP)
        box = (x0, y0, x0 + PARTY_TILE_WIDTH, y0 + PARTY_TILE_HEIGHT)
        slot_data = slots_in_order[i] if i < len(slots_in_order) else None
        _draw_party_tile(
            card, draw, box, slot_data, party_name_font, party_dex_font,
            party_slot_badge_font, party_level_badge_font, party_exp_font,
        )

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
