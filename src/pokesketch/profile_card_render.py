"""Player profile card rendering — the Pillow-rendered image that carries the
data density the merged `/profile` embed (cogs/profile.py) used to spread
across `embed.add_field(...)` calls.

`render_profile_panel()` is the one entry point: a pure, synchronous function
callable/testable without a live Discord interaction or any DB access — the
caller (cogs/profile.py's `_build_card_data()`) converts already-fetched DB
rows into a `ProfileCardData` before calling this. No I/O beyond loading the
system DejaVu fonts, so this is fully deterministic and unit-testable in
isolation.

Layout intent (see assets/user_profile_card/code.html for the mockup this
follows, and the module-level color tokens below, which are the LOCKED design
tokens for THIS render — not the raw Discord-Activity-UI palette dump in
assets/user_profile_card/DESIGN.md): an identity/header area, a gold-to-green
gradient EXP bar, a 3x2 grid of stat tiles (Global / Per-Server / Accuracy /
Pokédex / Shiny / Kudos), and a row of 6 party tiles (always 6 slots, empty
ones rendered as placeholders) with per-mon borders (gold for shiny, blurple
otherwise). Deliberately does NOT reproduce the mockup's full-bleed watermark
background (see rank_badges.py's module docstring for why that was dropped)
and does NOT render any per-mon level (`CaughtMon.mon_level` is a reserved/
unused fake stat elsewhere in this codebase — see cogs/profile.py — and this
render must not surface it either, even though the mockup shows "Lv. 85").
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

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

CARD_WIDTH = 900
MARGIN = 24
PANEL_RADIUS = 14

# --- Header -----------------------------------------------------------
HEADER_HEIGHT = 90
HEADER_FONT_SIZE = 30
HEADER_SUB_FONT_SIZE = 20

# --- EXP bar ------------------------------------------------------------
EXP_GAP_ABOVE = 16
EXP_LABEL_HEIGHT = 22
EXP_BAR_HEIGHT = 18
EXP_BAR_RADIUS = 9
EXP_SECTION_HEIGHT = EXP_GAP_ABOVE + EXP_LABEL_HEIGHT + EXP_BAR_HEIGHT

# --- Stat tile grid: 3 columns x 2 rows ---------------------------------
STAT_GAP_ABOVE = 20
STAT_COLS = 3
STAT_ROWS = 2
STAT_TILE_GAP = 12
STAT_TILE_HEIGHT = 92
STAT_TILE_WIDTH = (CARD_WIDTH - MARGIN * 2 - STAT_TILE_GAP * (STAT_COLS - 1)) // STAT_COLS
STAT_GRID_HEIGHT = STAT_TILE_HEIGHT * STAT_ROWS + STAT_TILE_GAP * (STAT_ROWS - 1)
STAT_LABEL_FONT_SIZE = 14
STAT_VALUE_FONT_SIZE = 24
STAT_SUB_FONT_SIZE = 14

# --- Party grid: always 6 slots ------------------------------------------
PARTY_GAP_ABOVE = 20
PARTY_LABEL_HEIGHT = 24
PARTY_SLOTS = 6
PARTY_TILE_GAP = 12
PARTY_TILE_HEIGHT = 92
PARTY_TILE_WIDTH = (CARD_WIDTH - MARGIN * 2 - PARTY_TILE_GAP * (PARTY_SLOTS - 1)) // PARTY_SLOTS
PARTY_TILE_BORDER_WIDTH = 2
PARTY_SECTION_HEIGHT = PARTY_LABEL_HEIGHT + PARTY_TILE_HEIGHT
PARTY_NAME_FONT_SIZE = 13
PARTY_DEX_FONT_SIZE = 12

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


def _draw_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    fill: tuple[int, int, int] = COLOR_SURFACE,
    outline: tuple[int, int, int] | None = None,
    outline_width: int = 1,
    radius: int = PANEL_RADIUS,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=outline_width)


def _draw_gradient_bar(card: Image.Image, box: tuple[int, int, int, int], frac: float) -> None:
    """Draw a rounded-rect EXP bar: a dim track, filled left-to-right with a
    horizontal gold-to-green gradient, composited through a rounded-rect
    alpha mask so a partially-filled bar still has rounded ends (built as a
    small gradient strip rather than a plain flat fill, per the locked design
    spec)."""
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
        r = int(COLOR_GOLD[0] + (COLOR_GREEN[0] - COLOR_GOLD[0]) * t)
        g = int(COLOR_GOLD[1] + (COLOR_GREEN[1] - COLOR_GOLD[1]) * t)
        b = int(COLOR_GOLD[2] + (COLOR_GREEN[2] - COLOR_GOLD[2]) * t)
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
        _draw_panel(draw, box, fill=COLOR_SURFACE, outline=COLOR_MUTED_BORDER, radius=10)
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
    _draw_panel(draw, box, fill=COLOR_SURFACE, outline=border, outline_width=PARTY_TILE_BORDER_WIDTH, radius=10)

    pad = 8
    max_w = (x1 - x0) - pad * 2
    dex_label = f"#{slot.dex_no:04d}"
    draw.text((x0 + pad, y0 + pad), dex_label, font=dex_font, fill=COLOR_SUBTEXT)

    name = slot.display_name + (" ★" if slot.is_shiny else "")
    name = _truncate_to_width(name, name_font, max_w)
    name_color = COLOR_GOLD if slot.is_shiny else COLOR_TEXT
    draw.text((x0 + pad, y0 + pad + 20), name, font=name_font, fill=name_color)

    draw.text((x0 + pad, y0 + pad + 44), f"Slot {slot.slot}", font=dex_font, fill=COLOR_SUBTEXT)


def render_profile_panel(data: ProfileCardData) -> bytes:
    """Composite the stat-grid + EXP-bar + party-grid profile card and
    return raw PNG bytes. Pure/synchronous/deterministic — no I/O beyond the
    system DejaVu fonts."""
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
    draw.text(
        (tx, ty + 38),
        f"{data.title_badge}  ·  Level {data.level}",
        font=sub_font,
        fill=COLOR_SUBTEXT,
    )

    # --- EXP bar -----------------------------------------------------
    exp_y = MARGIN + HEADER_HEIGHT + EXP_GAP_ABOVE
    label_text = f"{data.exp_current} / {data.exp_needed} EXP"
    draw.text((MARGIN, exp_y), label_text, font=exp_label_font, fill=COLOR_SUBTEXT)
    bar_y0 = exp_y + EXP_LABEL_HEIGHT
    bar_box = (MARGIN, bar_y0, CARD_WIDTH - MARGIN, bar_y0 + EXP_BAR_HEIGHT)
    frac = 0.0 if data.exp_needed <= 0 else data.exp_current / data.exp_needed
    _draw_gradient_bar(card, bar_box, frac)
    draw = ImageDraw.Draw(card)  # re-bind after paste() mutated the underlying image

    # --- Stat tile grid: Global / Per-Server / Accuracy / Pokédex / Shiny / Kudos
    grid_y = bar_y0 + EXP_BAR_HEIGHT + STAT_GAP_ABOVE

    def tile_box(col: int, row: int) -> tuple[int, int, int, int]:
        x0 = MARGIN + col * (STAT_TILE_WIDTH + STAT_TILE_GAP)
        y0 = grid_y + row * (STAT_TILE_HEIGHT + STAT_TILE_GAP)
        return (x0, y0, x0 + STAT_TILE_WIDTH, y0 + STAT_TILE_HEIGHT)

    _draw_stat_tile(
        card, draw, tile_box(0, 0), "Global",
        f"Lv {data.level}",
        f"Streak {data.global_streak} (best {data.global_streak_best}) · {data.global_sketch_count} sketches",
        stat_label_font, stat_value_font, stat_sub_font,
    )
    if data.server is not None:
        _draw_stat_tile(
            card, draw, tile_box(1, 0), "This Server",
            f"Lv {data.server.level}",
            f"Streak {data.server.streak} (best {data.server.streak_best}) "
            f"· {data.server.sketch_count} sketches",
            stat_label_font, stat_value_font, stat_sub_font,
        )
    else:
        _draw_stat_tile(
            card, draw, tile_box(1, 0), "This Server",
            "—",
            "No per-server data yet",
            stat_label_font, stat_value_font, stat_sub_font,
        )
    _draw_stat_tile(
        card, draw, tile_box(2, 0), "Accuracy",
        f"{data.accuracy_pct:.1f}%",
        None,
        stat_label_font, stat_value_font, stat_sub_font,
    )

    dex_pct = (data.dex_scanned / data.dex_total * 100) if data.dex_total else 0.0
    _draw_stat_tile(
        card, draw, tile_box(0, 1), "Pokédex",
        f"{data.dex_scanned}/{data.dex_total}",
        f"{dex_pct:.1f}% complete",
        stat_label_font, stat_value_font, stat_sub_font,
    )
    shiny_sub = f"Latest: {data.shiny_example}" if data.shiny_example else None
    _draw_stat_tile(
        card, draw, tile_box(1, 1), "Shiny",
        f"{data.shiny_count} species ★",
        shiny_sub,
        stat_label_font, stat_value_font, stat_sub_font,
    )
    _draw_stat_tile(
        card, draw, tile_box(2, 1), "Kudos",
        str(data.kudos_count),
        None,
        stat_label_font, stat_value_font, stat_sub_font,
    )

    # --- Party grid: always 6 slots ----------------------------------
    party_label_y = grid_y + STAT_GRID_HEIGHT + PARTY_GAP_ABOVE
    draw.text((MARGIN, party_label_y), "ACTIVE PARTY", font=section_label_font, fill=COLOR_SUBTEXT)
    party_y0 = party_label_y + PARTY_LABEL_HEIGHT
    slots_in_order = data.party[:PARTY_SLOTS]
    for i in range(PARTY_SLOTS):
        x0 = MARGIN + i * (PARTY_TILE_WIDTH + PARTY_TILE_GAP)
        box = (x0, party_y0, x0 + PARTY_TILE_WIDTH, party_y0 + PARTY_TILE_HEIGHT)
        slot_data = slots_in_order[i] if i < len(slots_in_order) else None
        _draw_party_tile(card, draw, box, slot_data, party_name_font, party_dex_font)

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
