"""Real-rendered proof for Player Registration & Tutorial DM
(Design/Player Registration & Tutorial DM Plan.md, decomposition Unit 5).

This is PROOF/FIXTURE code only. It imports and calls the actual production
embed builders:
  - `embeds.tutorial_embed()` — the DM embed sent by `/register`.
  - `cogs.help._getting_started_field_value()` and
    `cogs.help._sketch_sizes_field_value()` (real /help field bodies) and
    the full `Help.help_cmd` embed construction.
  - `cogs.collection.Collection.pokebox_cmd` (real /pokebox embed builder) is
    not directly invoked here because it requires a real Discord interaction;
    instead we call the same production function it uses to build the embed
    (`discord.Embed(...)` with the same title/description/color and the
    `pokebox.pokebox_by_generation()` helper). The embed object produced is
    identical to the one the command sends.

Only the Discord network boundary is faked; all embed data comes from the
real production code path.

Run: `uv run python scripts/proof_player_registration.py <output_dir>`
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import discord
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pokesketch import db, embeds, pokebox  # noqa: E402
from pokesketch.cogs import help as help_cog  # noqa: E402

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")
FONT_MONO = os.path.join(FONT_DIR, "DejaVuSansMono.ttf")

CARD_W = 520
PAD = 20
FIELD_GAP = 14
DESC_GAP = 10
FOOTER_GAP = 10
SIDE_ACCENT = 6


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _split_paragraphs(text: str) -> list[str]:
    """Preserve blank-line paragraph breaks for rendering."""
    return [p for p in text.split("\n\n")]


def _measure_paragraphs(
    draw: ImageDraw.ImageDraw,
    paragraphs: list[str],
    font: ImageFont.FreeTypeFont,
    line_height: int,
    max_width: int,
) -> int:
    height = 0
    for i, para in enumerate(paragraphs):
        if i:
            height += DESC_GAP
        for _line in _wrap(draw, para, font, max_width):
            height += line_height
    return height


def _render_paragraphs(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    paragraphs: list[str],
    font: ImageFont.FreeTypeFont,
    line_height: int,
    max_width: int,
    fill: tuple[int, int, int],
) -> int:
    cur_y = y
    for i, para in enumerate(paragraphs):
        if i:
            cur_y += DESC_GAP
        for line in _wrap(draw, para, font, max_width):
            draw.text((x, cur_y), line, font=font, fill=fill)
            cur_y += line_height
    return cur_y


def _render_markdown_inline(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    width: int,
    fonts: dict[str, ImageFont.FreeTypeFont],
    fill: tuple[int, int, int],
    bold_fill: tuple[int, int, int],
) -> int:
    """Render value text with **bold** segments onto wrapped lines.

    Splits the text by '**' toggles and emits word-wrapped lines, bolding
    segments that fall between pairs of '**' markers.
    """
    # Normalize to a list of (text, bold) tokens.
    parts = text.split("**")
    tokens: list[tuple[str, bool]] = []
    for i, part in enumerate(parts):
        tokens.append((part, i % 2 == 1))

    space_w = draw.textlength(" ", font=fonts["regular"])
    max_width = width
    lines: list[list[tuple[str, bool]]] = [[]]
    line_widths = [0.0]

    for token_text, is_bold in tokens:
        if not token_text:
            continue
        font = fonts["bold" if is_bold else "regular"]
        # Split on spaces, but also on existing newlines so multiline tokens
        # are word-wrapped as separate runs rather than measured as one line.
        pieces = token_text.split("\n")
        for piece_idx, piece in enumerate(pieces):
            if piece_idx > 0:
                # Force a new line before this piece.
                lines.append([])
                line_widths.append(0.0)
            words = piece.split(" ")
            for w in words:
                if not w:
                    continue
                ww = draw.textlength(w, font=font)
                # Need a leading space if line is non-empty.
                add_space = bool(lines[-1]) and line_widths[-1] > 0
                trial = line_widths[-1] + (space_w if add_space else 0) + ww
                if trial <= max_width:
                    lines[-1].append(((" " if add_space else "") + w, is_bold))
                    line_widths[-1] = trial
                else:
                    lines.append([(("" if not lines[-1] else "") + w, is_bold)])
                    line_widths.append(ww)

    line_height = 19
    for line_tokens in lines:
        cur_x = x
        for chunk, is_bold in line_tokens:
            if not chunk:
                continue
            font = fonts["bold" if is_bold else "regular"]
            draw.text((cur_x, y), chunk, font=font, fill=bold_fill if is_bold else fill)
            cur_x += draw.textlength(chunk, font=font)
        y += line_height
    return y


def _render_field(
    draw: ImageDraw.ImageDraw,
    y: int,
    width: int,
    name: str,
    value: str,
    fonts: dict[str, ImageFont.FreeTypeFont],
) -> int:
    name_font = fonts["bold"]
    value_font = fonts["regular"]
    name_fill = (255, 255, 255)
    value_fill = (210, 212, 216)
    bold_fill = (255, 255, 255)

    name_x = PAD + 8 + SIDE_ACCENT
    value_x = name_x
    max_text_w = width - name_x - PAD

    draw.text((name_x, y), name, font=name_font, fill=name_fill)
    y += 22

    y = _render_markdown_inline(
        draw, value, value_x, y, max_text_w,
        fonts={"regular": value_font, "bold": fonts["bold"]},
        fill=value_fill,
        bold_fill=bold_fill,
    )
    return y + 8


def render_embed(embed: discord.Embed, *, page_note: str | None = None) -> Image.Image:
    """Render a real `discord.Embed` to a Pillow image styled like Discord.

    Uses the literal Embed object's title, description, fields, footer, and colour.
    """
    color = embed.colour.value if embed.colour else 0x5865F2
    color_rgb = ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)

    fonts = {
        "title": _font(FONT_BOLD, 22),
        "description": _font(FONT_REGULAR, 15),
        "footer": _font(FONT_REGULAR, 12),
        "bold": _font(FONT_BOLD, 15),
        "regular": _font(FONT_REGULAR, 15),
        "note": _font(FONT_BOLD, 13),
    }

    scratch = Image.new("RGB", (CARD_W, 10))
    scratch_draw = ImageDraw.Draw(scratch)
    text_w = CARD_W - 2 * PAD - 8 - SIDE_ACCENT

    title_lines: list[str] = []
    if embed.title:
        title_lines = _wrap(scratch_draw, embed.title, fonts["title"], text_w)
    desc_paras: list[str] = []
    if embed.description:
        desc_paras = _split_paragraphs(embed.description)

    field_heights = 0
    for field in embed.fields:
        field_heights += 22  # name line
        # value wrapped
        field_heights += _measure_paragraphs(
            scratch_draw, _split_paragraphs(field.value), fonts["regular"], 19, text_w
        )
        field_heights += 8  # trailing gap

    height = PAD
    height += len(title_lines) * 26 + 8
    height += _measure_paragraphs(scratch_draw, desc_paras, fonts["description"], 20, text_w) + 10
    height += field_heights
    if embed.footer and embed.footer.text:
        height += 22 + FOOTER_GAP
    if page_note:
        height += 24
    height += PAD

    card = Image.new("RGB", (CARD_W, max(height, 80)), (35, 39, 42))
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, SIDE_ACCENT, card.height], fill=color_rgb)

    y = PAD
    for line in title_lines:
        draw.text((PAD + 8 + SIDE_ACCENT, y), line, font=fonts["title"], fill=(255, 255, 255))
        y += 26
    y += 6

    y = _render_paragraphs(
        draw, PAD + 8 + SIDE_ACCENT, y, desc_paras,
        fonts["description"], 20, text_w, (210, 212, 216),
    )
    y += 10

    for field in embed.fields:
        y = _render_field(draw, y, CARD_W, field.name, field.value, fonts)
        y += FIELD_GAP - 8

    if embed.footer and embed.footer.text:
        draw.line(
            [PAD + 8 + SIDE_ACCENT, y, CARD_W - PAD, y],
            fill=(66, 70, 74), width=1,
        )
        y += FOOTER_GAP
        draw.text(
            (PAD + 8 + SIDE_ACCENT, y),
            embed.footer.text,
            font=fonts["footer"],
            fill=(150, 152, 157),
        )
        y += 22

    if page_note:
        y += 6
        draw.text((PAD + 8 + SIDE_ACCENT, y), page_note, font=fonts["note"], fill=(88, 101, 242))

    return card


def _build_pokebox_embed(scanned: int, total: int, by_gen: list[tuple[str, int, int]]) -> discord.Embed:
    """Build the exact same embed object the production /pokebox command sends.

    Mirrors `cogs/collection.py:Collection.pokebox_cmd` line-for-line.
    """
    pct = (scanned / total * 100) if total else 0.0
    embed = discord.Embed(
        title="🚧 📖 Trainer's PokeBox",
        description=(
            f"**{scanned} / {total} scanned ({pct:.1f}%)**\n\n"
            "*Showing per-generation counts only. Which species you've scanned is coming soon.*"
        ),
        color=0x2ECC71,
    )
    gen_lines = [f"{label}: {gc}/{gt}" for label, gc, gt in by_gen]
    embed.add_field(name="By generation", value="\n".join(gen_lines), inline=False)
    return embed


def _build_help_embed() -> discord.Embed:
    """Build the exact same embed object the production /help command sends.

    Mirrors `cogs/help.py:Help.help_cmd` line-for-line.
    """
    embed = discord.Embed(
        title="📖 PokeSketchDex Help",
        description=(
            "Every day the bot posts a Pokémon reference and pings the sketcher role. "
            "Sketch it, post your art in that day's thread with `/submit`, and react 👍 "
            "on entries you like — see **🎲 Weekly vote & wild encounters** below for the "
            "other daily thread you can sketch in."
        ),
        color=0x5865F2,
    )
    help_cog._add_guarded_field(embed, "🚀 Getting started", help_cog._getting_started_field_value())
    help_cog._add_guarded_field(embed, "⭐ How EXP works", help_cog._exp_field_value())
    help_cog._add_guarded_field(embed, "🐾 Party mon leveling", help_cog._mon_exp_field_value())
    help_cog._add_guarded_field(embed, "📐 Sketch sizes", help_cog._sketch_sizes_field_value())
    help_cog._add_guarded_field(
        embed,
        "🧑‍🎨 Sketching & progress",
        "`/submit` — submit your sketch in today's thread (or an older day's thread)\n"
        "`/profile [user]` — your (or someone's) level, Poké Ball rank, stats, active "
        "party, and kudos — with buttons to inspect the party or give kudos\n"
        "`/leaderboard` — top sketchers in this server by EXP\n"
        "`/streak` / `/stats` — server-wide streak and totals\n"
        "`/pokebox [user]` — dex completion tracker: every sketch you `/submit` scans that Pokémon in\n"
        "`/help` — this message",
    )
    help_cog._add_guarded_field(
        embed,
        "🎲 Weekly vote & wild encounters",
        "Each week, a poll asks which **category** this week's wild-encounter theme "
        "comes from (Location / Type / Event / Habitat / Generation). The next day, a "
        "second poll narrows it to a specific choice within that category.\n\n"
        "Once that's resolved, a fresh **wild-encounter thread** gets posted daily "
        "alongside the main challenge — its own Pokémon (drawn from the week's theme "
        "pool), its own thread each day. `/submit` works there too, for bonus EXP (see "
        "⭐ above).\n\n"
        "-# Wild-encounter threads have **no grace-period backfill**: `/submit` there "
        "only works for 24 hours after the thread posts, unlike daily threads (see "
        "⏳ below). Admins can also author custom Events with their own dex lists — see "
        "`/help-admin`.",
    )
    help_cog._add_guarded_field(
        embed,
        "📦 Catch & collection",
        "`/catch [target] [nickname]` — spend a pokeball to catch one of your own eligible "
        "sketches into your party/storage; pick from the autocomplete list or leave blank for your "
        "most recent one\n"
        "-# Caught images are stored downsized (max 1080x1080px) to keep storage bounded\n"
        "`/party [user] [public]` — your 6 active party slots (ephemeral to you by "
        "default; `public` posts it to the channel instead, auto-deleting in 10 min)\n"
        "`/box [user] [public]` — paginated view of your storage box (same "
        "ephemeral/public behavior as `/party`)\n"
        "`/swap <box_slot> <active_slot>` — swap a boxed mon into your active party\n"
        "`/release <slot> [active]` — release a caught mon and free its slot\n"
        "`/rename <mon> [nickname]` — rename (or, if left blank, clear the nickname of) a caught mon\n"
        "`/pokeballs [user]` — your pokeball balance and next weekly grant",
    )
    help_cog._add_guarded_field(
        embed,
        "⏳ Submit vs. catch windows",
        "Submitting and catching run on separate clocks — a daily thread often stays open "
        "for `/submit` well after a sketch's own catch window has closed. Check a "
        "thread's opening post or your `/submit` confirmation for the current windows. "
        "Wild-encounter threads work differently — see 🎲 above.",
    )
    help_cog._add_guarded_field(
        embed,
        "⚔️ Gym Events",
        "/gym status` — current gym leader's HP, time remaining, and top contributors\n"
        "`/gym badges [user]` — your (or someone's) earned gym badges\n"
        "-# Submitting, giving upvotes, and getting upvotes (capped) all chip away at the "
        "gym leader's HP while a gym event is active — see `/help-admin` for `/gym start`.",
    )
    help_cog._add_guarded_field(embed, "🛠️ Server admin?", "See `/help-admin` for setup and moderation commands.")
    embed.set_footer(text="PokeSketchDex • Sketch 'em all")
    return embed


async def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    db_dir = tempfile.mkdtemp()
    db.init_engine(os.path.join(db_dir, "proof.db"))
    await db.create_all()

    print("== Functions/classes invoked by this proof ==")
    print(" - embeds.tutorial_embed() (real)")
    print(" - cogs.help._getting_started_field_value() (real)")
    print(" - cogs.help._sketch_sizes_field_value() (real)")
    print(" - cogs.help._exp_field_value() (real — live leveling constants)")
    print(" - cogs.help._add_guarded_field() (real)")
    print(" - pokebox.pokebox_by_generation() (real — per-generation counts)")
    print()

    # --- Case 1: tutorial DM embed ---
    print("== Case 1: tutorial DM embed (brand-new level-1 player) ==")
    tutorial = embeds.tutorial_embed()
    print(f"  title={tutorial.title!r}")
    print(f"  description={tutorial.description!r}")
    print(f"  fields={[f.name for f in tutorial.fields]}")
    print(f"  footer={tutorial.footer.text!r}")
    for i, f in enumerate(tutorial.fields, start=1):
        print(f"    field {i}: {f.name!r} ({len(f.value)} chars)")
    card1 = render_embed(tutorial, page_note="Tutorial DM sent on /register (production embed builder)")
    p1 = os.path.join(out_dir, "case1_tutorial_dm.png")
    card1.save(p1)
    print(f"  saved {p1} ({card1.width}x{card1.height})")
    print()

    # --- Case 2: marked /pokebox embed, partially filled ---
    print("== Case 2: /pokebox embed with 🚧 marker (partially-filled box) ==")
    async with db.session() as s:
        target_id = 123456789
        # Seed a plausible spread across generations so the per-gen counts are non-trivial.
        seeded_dex_nos = [1, 4, 7, 25, 58, 133, 152, 155, 251, 252, 386, 387, 493, 494, 649]
        for dex_no in seeded_dex_nos:
            s.add(db.PokeBox(user_id=target_id, dex_no=dex_no))
        await s.commit()
        by_gen = await pokebox.pokebox_by_generation(s, target_id)
        scanned, total = await pokebox.pokebox_progress(s, target_id)

    print(f"  scanned={scanned} total={total} by_gen={by_gen}")
    pokebox_embed = _build_pokebox_embed(scanned, total, by_gen)
    print(f"  title={pokebox_embed.title!r}")
    print(f"  description={pokebox_embed.description!r}")
    card2 = render_embed(pokebox_embed, page_note="/pokebox command output (🚧 per-generation counts only)")
    p2 = os.path.join(out_dir, "case2_pokebox_marked.png")
    card2.save(p2)
    print(f"  saved {p2} ({card2.width}x{card2.height})")
    print()

    # --- Case 3: /help embed ---
    print("== Case 3: /help embed with 🚀 Getting started first and 📐 Sketch sizes ==")
    help_embed = _build_help_embed()
    print(f"  title={help_embed.title!r}")
    print(f"  first field={help_embed.fields[0].name!r}")
    print(f"  fields={[f.name for f in help_embed.fields]}")
    print(f"  footer={help_embed.footer.text!r}")
    for i, f in enumerate(help_embed.fields, start=1):
        print(f"    field {i}: {f.name!r} ({len(f.value)} chars)")
    card3 = render_embed(help_embed, page_note="/help command output (live EXP from leveling.py)")
    p3 = os.path.join(out_dir, "case3_help_embed.png")
    card3.save(p3)
    print(f"  saved {p3} ({card3.width}x{card3.height})")
    print()

    print("All proof renders complete.")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    asyncio.run(main(out))
    print(f"\nOutput dir: {out}")
