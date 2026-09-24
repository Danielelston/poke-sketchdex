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
dropped) and does NOT render any per-mon level (`CaughtMon.mon_level` is a
reserved/unused fake stat elsewhere in this codebase — see cogs/profile.py —
and this render must not surface it either, even though the mockup shows
"Lv. 85").
"""

from __future__ import annotations

import colorsys
import io
import logging
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

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
HEADER_HEIGHT = 90
HEADER_FONT_SIZE = 30
HEADER_SUB_FONT_SIZE = 20

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
PARTY_TILE_HEIGHT = 104
PARTY_TILE_WIDTH = (CARD_WIDTH - MARGIN * 2 - PARTY_TILE_GAP * (PARTY_COLS - 1)) // PARTY_COLS
PARTY_TILE_BORDER_WIDTH = 2
PARTY_SECTION_HEIGHT = (
    PARTY_LABEL_HEIGHT + PARTY_TILE_HEIGHT * PARTY_ROWS + PARTY_TILE_GAP * (PARTY_ROWS - 1)
)
PARTY_NAME_FONT_SIZE = 14
PARTY_DEX_FONT_SIZE = 12
PARTY_SPRITE_SIZE = 44
PARTY_SLOT_BADGE_SIZE = 26  # corner-ribbon badge, not a circle — see _draw_corner_ribbon_badge
PARTY_TILE_RADIUS = 10  # must match the radius passed to _draw_panel() for party tiles

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
    display_name: str
    is_shiny: bool
    # Local file path to a cached official-artwork PNG (fetched by the async
    # caller via pokeapi.PokeApiClient.get_sprite_image_path — this render
    # module stays synchronous and just opens+composites it), or None if no
    # sprite was fetched/available.
    sprite_path: str | None = None


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
    _draw_panel(
        draw, box, fill=COLOR_SURFACE, outline=border, outline_width=PARTY_TILE_BORDER_WIDTH,
        radius=PARTY_TILE_RADIUS,
    )

    pad = 8
    max_w = (x1 - x0) - pad * 2
    cx = x0 + (x1 - x0) // 2

    sprite_top = y0 + pad
    if slot.sprite_path:
        try:
            with Image.open(slot.sprite_path) as sprite_img:
                sprite_img = sprite_img.convert("RGBA").resize(
                    (PARTY_SPRITE_SIZE, PARTY_SPRITE_SIZE), Image.Resampling.LANCZOS
                )
                card.paste(sprite_img, (cx - PARTY_SPRITE_SIZE // 2, sprite_top), sprite_img)
        except Exception:
            log.warning("Failed to composite party sprite from %r", slot.sprite_path, exc_info=True)

    # Species name first, dex number below it (swapped order from the
    # previous layout).
    text_top = sprite_top + PARTY_SPRITE_SIZE + 6
    name = slot.display_name + (" ★" if slot.is_shiny else "")
    name = _truncate_to_width(name, name_font, max_w)
    name_color = COLOR_GOLD if slot.is_shiny else COLOR_TEXT
    name_bbox = draw.textbbox((0, 0), name, font=name_font)
    draw.text((cx - (name_bbox[2] - name_bbox[0]) // 2, text_top), name, font=name_font, fill=name_color)

    dex_label = f"#{slot.dex_no:04d}"
    dex_bbox = draw.textbbox((0, 0), dex_label, font=dex_font)
    draw.text(
        (cx - (dex_bbox[2] - dex_bbox[0]) // 2, text_top + 18), dex_label, font=dex_font, fill=COLOR_SUBTEXT
    )

    # Slot number as a small corner-ribbon badge (top-right) instead of a
    # plain circle — flush with the tile's own rounded corner, concave inner
    # arc, per the mockup reference the user supplied.
    badge_text_color = COLOR_BASE if slot.is_shiny else COLOR_TEXT
    _draw_corner_ribbon_badge(
        card, box, str(slot.slot), dex_font, bg_color=border, text_color=badge_text_color
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
    section_label_font = ImageFont.truetype(FONT_BOLD, 15)

    # --- Header / identity area ------------------------------------
    header_box = (MARGIN, MARGIN, CARD_WIDTH - MARGIN, MARGIN + HEADER_HEIGHT)
    _draw_panel(draw, header_box, fill=COLOR_SURFACE, outline=COLOR_MUTED_BORDER)
    tx = MARGIN + 16
    ty = MARGIN + 14
    username = _truncate_to_width(data.username, header_font, CARD_WIDTH - MARGIN * 2 - 32)
    draw.text((tx, ty), username, font=header_font, fill=COLOR_TEXT)

    # Sub-line: "Level {N} · {title_badge}" (title_badge rendered as a
    # chip, not plain inline text).
    sub_y = ty + 38
    level_text = f"Level {data.level} · "
    draw.text((tx, sub_y), level_text, font=sub_font, fill=COLOR_SUBTEXT)
    chip_x = tx + int(sub_font.getlength(level_text))
    chip_right_limit = CARD_WIDTH - MARGIN - 16
    chip_max_text_w = max(0, (chip_right_limit - chip_x) - CHIP_PAD_X * 2)
    badge_text = _truncate_to_width(data.title_badge, sub_font, chip_max_text_w)
    _draw_chip(draw, chip_x, sub_y - CHIP_PAD_Y, badge_text, sub_font, COLOR_BLURPLE, COLOR_TEXT)

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
    # Accuracy tile): global streak always shown, per-server streak on a
    # second line (or the established "no per-server data" convention when
    # data.server is None, same as the This Server tile above). Uses a
    # compact "G:14 (best 31)" format (not "Global: 14 (best 31)") since the
    # narrower mobile-layout tile can't fit the verbose form at the stat
    # value font size without truncating.
    streak_subtext = (
        f"S: {data.server.streak} (best {data.server.streak_best})"
        if data.server is not None
        else "No per-server data yet"
    )
    _draw_stat_tile(
        card, draw, tile_box(0, 1), "Streak",
        f"G: {data.global_streak} (best {data.global_streak_best})",
        streak_subtext,
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
        _draw_party_tile(card, draw, box, slot_data, party_name_font, party_dex_font)

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
