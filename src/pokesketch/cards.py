"""Player profile card rendering.

`render_profile_card()` is the one entry point: a pure, synchronous function
callable/testable without a live Discord interaction — same separation of
concerns as pokebox.py's `_normalize_and_save`. All I/O (PokeAPI sprite
fetches, tier-asset pre-rendering) happens in the caller (cogs/profile_card.py)
before this is invoked, so a future render-cache wrapper can sit in front of
it without a rearchitecture (locked v1 behavior: no cache, render every call).
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from . import rank_badges

CARD_WIDTH = 900
CARD_HEIGHT = 600
CARD_SIZE = (CARD_WIDTH, CARD_HEIGHT)

BG_COLOR = (30, 30, 40, 255)
TEXT_COLOR = (255, 255, 255, 255)
SUBTEXT_COLOR = (200, 200, 210, 255)
BAR_BG_COLOR = (60, 60, 75, 255)
BAR_FILL_COLOR = (88, 101, 242, 255)  # Discord blurple

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

MARGIN = 32
PARTY_THUMB_SIZE = 110
PARTY_THUMB_SPACING = 16
MAX_PARTY_SLOTS = 6
CORNER_EMBLEM_MARGIN = 24
EXP_BAR_HEIGHT = 24
EXP_BAR_RADIUS = 8

# Section panels: solid, rounded-rect backdrops drawn fresh on every render
# (NOT pre-rendered art) behind each content area — header/stats block and
# the party row — so text and sprites always sit on a readable solid surface
# regardless of what the full-bleed ball-silhouette watermark looks like
# underneath. Drawn programmatically rather than baked into the per-tier
# background art because panel size/position needs to adapt to content (the
# party panel is simply skipped when there's no active party, and panel
# width already has to account for the pixel-width-truncated username) —
# baking fixed-size panels into pre-rendered tier art can't flex for that.
# Tinted per-tier via rank_badges.accent_color_for_tier() so the card still
# reads as tier-branded despite the panels themselves being generic shapes.
PANEL_RADIUS = 20
PANEL_ALPHA = 235
PANEL_BASE_COLOR = (24, 24, 32)
PANEL_ACCENT_BORDER_ALPHA = 160
PANEL_ACCENT_BORDER_WIDTH = 3
PANEL_PADDING = 20

HEADER_FONT_SIZE = 42
# Reserve room on the right for the corner emblem (see render_profile_card)
# so a long username can never overlap it — pixel-width-aware, not a fixed
# character count, since DejaVu Bold's glyph widths vary a lot by character.
HEADER_MAX_WIDTH = CARD_WIDTH - MARGIN * 2 - PANEL_PADDING * 2 - (CORNER_EMBLEM_MARGIN * 2 + 64)


def _draw_panel(
    card: Image.Image, box: tuple[int, int, int, int], accent: tuple[int, int, int]
) -> None:
    """Draw one solid, rounded-rect section panel (a translucent dark card,
    not pre-rendered art) at `box`, with a thin accent-colored border tying
    it to the current rank tier. Alpha-composited so the panel's translucency
    lets a hint of the full-bleed watermark show through at the edges rather
    than fully occluding it."""
    x0, y0, x1, y1 = box
    panel = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle(
        (0, 0, x1 - x0 - 1, y1 - y0 - 1),
        radius=PANEL_RADIUS,
        fill=(*PANEL_BASE_COLOR, PANEL_ALPHA),
        outline=(*accent, PANEL_ACCENT_BORDER_ALPHA),
        width=PANEL_ACCENT_BORDER_WIDTH,
    )
    card.alpha_composite(panel, (x0, y0))


def _truncate_to_width(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    """Truncate `text` with a trailing ellipsis so its rendered width (via this
    exact font) fits within `max_width` — measures real glyph widths rather
    than assuming a fixed character-count budget, since bold display fonts
    vary a lot in per-character width and a char-count guard alone can still
    overrun the canvas (confirmed by manual visual review of sample renders
    before merge: a 32-char username at 42pt bold ran past the canvas edge
    and collided with the corner emblem)."""
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


def _load_party_thumbnail(path: str) -> Image.Image:
    """Load + normalize a party sprite image to a fixed-size square RGBA
    thumbnail, letterboxed (never cropped or stretched) so inconsistent
    source aspect ratios don't distort — mirrors pokebox.py's
    `_normalize_and_save` resize approach."""
    with Image.open(path) as img:
        img = img.convert("RGBA")
        img.thumbnail((PARTY_THUMB_SIZE, PARTY_THUMB_SIZE), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (PARTY_THUMB_SIZE, PARTY_THUMB_SIZE), (0, 0, 0, 0))
        offset = ((PARTY_THUMB_SIZE - img.width) // 2, (PARTY_THUMB_SIZE - img.height) // 2)
        canvas.alpha_composite(img, offset)
        return canvas


def render_profile_card(
    username: str,
    level: int,
    rank_tier: str,
    exp_current: int,
    exp_needed: int,
    streak: int,
    submission_count: int,
    party_sprite_paths: list[str],
    tier_corner_emblem_path: str,
    tier_full_bleed_path: str,
) -> bytes:
    """Composite a player profile card and return raw PNG bytes.

    All inputs are plain data or local file paths — no DB/Discord objects —
    so this is fully unit-testable. A missing/unreadable party sprite path is
    skipped rather than raising, since sprite availability is outside the
    caller's control (a cold PokeAPI miss shouldn't break the whole card).
    """
    accent = rank_badges.accent_color_for_tier(rank_tier)
    card = Image.new("RGBA", CARD_SIZE, BG_COLOR)

    with Image.open(tier_full_bleed_path) as bg:
        card.alpha_composite(bg.convert("RGBA"))

    draw = ImageDraw.Draw(card)
    header_font = ImageFont.truetype(FONT_BOLD, HEADER_FONT_SIZE)
    subheader_font = ImageFont.truetype(FONT_BOLD, 26)
    stat_font = ImageFont.truetype(FONT_REGULAR, 24)

    display_username = _truncate_to_width(username, header_font, HEADER_MAX_WIDTH)

    text_x = MARGIN + PANEL_PADDING
    text_y = MARGIN + PANEL_PADDING

    bar_y = text_y + 100
    bar_w = CARD_WIDTH - (MARGIN + PANEL_PADDING) * 2
    stats_y = bar_y + EXP_BAR_HEIGHT + 44
    submitted_y = stats_y + 32

    # Header/stats panel: a solid backdrop behind the username, level/rank
    # line, EXP bar, and stat lines, so that block is always readable
    # regardless of the watermark underneath (see PANEL_* constants above
    # for why this is drawn fresh here rather than pre-rendered). Height is
    # measured from the LAST line's actual rendered bbox (not a hardcoded
    # constant) so the panel can never clip its own content if line spacing,
    # font sizes, or content ever change — confirmed by manual visual review
    # of sample renders before merge: an earlier fixed-height panel clipped
    # the last "Sketches submitted" stat line by ~15-30px.
    last_line_bbox = draw.textbbox((text_x, submitted_y), "Sketches submitted: 0", font=stat_font)
    header_panel_bottom = int(last_line_bbox[3]) + PANEL_PADDING
    header_panel_box = (MARGIN, MARGIN, CARD_WIDTH - MARGIN, header_panel_bottom)
    _draw_panel(card, header_panel_box, accent)

    with Image.open(tier_corner_emblem_path) as emblem:
        emblem = emblem.convert("RGBA")
        pos = (CARD_WIDTH - emblem.width - CORNER_EMBLEM_MARGIN, CORNER_EMBLEM_MARGIN)
        card.alpha_composite(emblem, pos)

    draw.text((text_x, text_y), display_username, font=header_font, fill=TEXT_COLOR)
    draw.text(
        (text_x, text_y + 56), f"Level {level} · {rank_tier}", font=subheader_font, fill=SUBTEXT_COLOR
    )

    draw.rounded_rectangle(
        (text_x, bar_y, text_x + bar_w, bar_y + EXP_BAR_HEIGHT), radius=EXP_BAR_RADIUS, fill=BAR_BG_COLOR
    )
    frac = 0.0 if exp_needed <= 0 else min(1.0, exp_current / exp_needed)
    filled_w = int(bar_w * frac)
    if filled_w > 0:
        draw.rectangle((text_x, bar_y, text_x + filled_w, bar_y + EXP_BAR_HEIGHT), fill=BAR_FILL_COLOR)
    draw.text(
        (text_x, bar_y + EXP_BAR_HEIGHT + 8), f"{exp_current} / {exp_needed} EXP", font=stat_font, fill=SUBTEXT_COLOR
    )

    day_word = "day" if streak == 1 else "days"
    draw.text((text_x, stats_y), f"Streak: {streak} {day_word}", font=stat_font, fill=SUBTEXT_COLOR)
    draw.text(
        (text_x, submitted_y), f"Sketches submitted: {submission_count}", font=stat_font, fill=SUBTEXT_COLOR
    )

    slots = party_sprite_paths[:MAX_PARTY_SLOTS]
    thumbnails = []
    for path in slots:
        try:
            thumbnails.append(_load_party_thumbnail(path))
        except (OSError, ValueError):
            continue

    if thumbnails:
        # Party panel: only drawn when there's an active party to show — a
        # fixed pre-rendered panel couldn't conditionally disappear like this.
        party_panel_h = PARTY_THUMB_SIZE + PANEL_PADDING * 2
        party_panel_box = (
            MARGIN,
            CARD_HEIGHT - MARGIN - party_panel_h,
            CARD_WIDTH - MARGIN,
            CARD_HEIGHT - MARGIN,
        )
        _draw_panel(card, party_panel_box, accent)

        party_y = party_panel_box[1] + PANEL_PADDING
        total_w = len(thumbnails) * PARTY_THUMB_SIZE + (len(thumbnails) - 1) * PARTY_THUMB_SPACING
        start_x = max(MARGIN + PANEL_PADDING, (CARD_WIDTH - total_w) // 2)
        for i, thumb in enumerate(thumbnails):
            x = start_x + i * (PARTY_THUMB_SIZE + PARTY_THUMB_SPACING)
            card.alpha_composite(thumb, (x, party_y))

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
