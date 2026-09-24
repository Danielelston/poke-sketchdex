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

HEADER_FONT_SIZE = 42
# Reserve room on the right for the corner emblem (see render_profile_card)
# so a long username can never overlap it — pixel-width-aware, not a fixed
# character count, since DejaVu Bold's glyph widths vary a lot by character.
HEADER_MAX_WIDTH = CARD_WIDTH - MARGIN - (CORNER_EMBLEM_MARGIN * 2 + 64)


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
    card = Image.new("RGBA", CARD_SIZE, BG_COLOR)

    with Image.open(tier_full_bleed_path) as bg:
        card.alpha_composite(bg.convert("RGBA"))

    with Image.open(tier_corner_emblem_path) as emblem:
        emblem = emblem.convert("RGBA")
        pos = (CARD_WIDTH - emblem.width - CORNER_EMBLEM_MARGIN, CORNER_EMBLEM_MARGIN)
        card.alpha_composite(emblem, pos)

    draw = ImageDraw.Draw(card)
    header_font = ImageFont.truetype(FONT_BOLD, HEADER_FONT_SIZE)
    subheader_font = ImageFont.truetype(FONT_BOLD, 26)
    stat_font = ImageFont.truetype(FONT_REGULAR, 24)

    display_username = _truncate_to_width(username, header_font, HEADER_MAX_WIDTH)

    draw.text((MARGIN, MARGIN), display_username, font=header_font, fill=TEXT_COLOR)
    draw.text((MARGIN, MARGIN + 56), f"Level {level} · {rank_tier}", font=subheader_font, fill=SUBTEXT_COLOR)

    bar_y = MARGIN + 100
    bar_w = CARD_WIDTH - MARGIN * 2
    draw.rounded_rectangle(
        (MARGIN, bar_y, MARGIN + bar_w, bar_y + EXP_BAR_HEIGHT), radius=EXP_BAR_RADIUS, fill=BAR_BG_COLOR
    )
    frac = 0.0 if exp_needed <= 0 else min(1.0, exp_current / exp_needed)
    filled_w = int(bar_w * frac)
    if filled_w > 0:
        draw.rectangle((MARGIN, bar_y, MARGIN + filled_w, bar_y + EXP_BAR_HEIGHT), fill=BAR_FILL_COLOR)
    draw.text(
        (MARGIN, bar_y + EXP_BAR_HEIGHT + 8), f"{exp_current} / {exp_needed} EXP", font=stat_font, fill=SUBTEXT_COLOR
    )

    stats_y = bar_y + EXP_BAR_HEIGHT + 44
    day_word = "day" if streak == 1 else "days"
    draw.text((MARGIN, stats_y), f"Streak: {streak} {day_word}", font=stat_font, fill=SUBTEXT_COLOR)
    draw.text((MARGIN, stats_y + 32), f"Sketches submitted: {submission_count}", font=stat_font, fill=SUBTEXT_COLOR)

    slots = party_sprite_paths[:MAX_PARTY_SLOTS]
    thumbnails = []
    for path in slots:
        try:
            thumbnails.append(_load_party_thumbnail(path))
        except (OSError, ValueError):
            continue

    if thumbnails:
        party_y = CARD_HEIGHT - PARTY_THUMB_SIZE - MARGIN
        total_w = len(thumbnails) * PARTY_THUMB_SIZE + (len(thumbnails) - 1) * PARTY_THUMB_SPACING
        start_x = max(MARGIN, (CARD_WIDTH - total_w) // 2)
        for i, thumb in enumerate(thumbnails):
            x = start_x + i * (PARTY_THUMB_SIZE + PARTY_THUMB_SPACING)
            card.alpha_composite(thumb, (x, party_y))

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
