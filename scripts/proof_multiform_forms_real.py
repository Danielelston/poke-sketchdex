"""Real-rendered proof for the alt-forms picker, against LIVE PokeAPI data
(Design/Multiform Pokemon Reference Picker Plan.md, decomposition Unit 4).

Unlike `scripts/proof_multiform_forms.py` (which mocks the PokeAPI HTTP
transport with hand-built fixtures, including a *synthetic* 63-variety
Alcremie fixture that does not match real PokeAPI data), this script makes
NO fake data and NO mocked HTTP transport at all. Every fetch —
`pokemon-species/{id}` and `pokemon/{id}` — is a real network call to
https://pokeapi.co, through a fresh on-disk cache dir so nothing is served
from a stale cache. The production code under test
(`pokeapi.PokeApiClient.get_species_forms`, `daily._forms_view_for_dex`,
`ui.FormsButtonView`, `ui._FormsPaginatorView`, `ui._build_form_pages`) is
imported and called completely unmodified.

Species chosen after verifying live PokeAPI data first (see kanban card
t_82654757 and the design doc's 2026-09-26 "real-data gap" status-log
entry): Alcremie's own `pokemon-species.varieties` has only 2 entries in
real data (not ~63 — that count lives under `pokemon.forms`, which this
feature does not read), so it cannot demonstrate capping. Minior (774) has
14 REAL varieties (7 meteor-shell colors + 7 core colors, no battle-only
suffixes), confirmed via a live GET immediately before writing this script
— it demonstrates the >12 capped/sampled case correctly.

Cases proven (see card acceptance criteria):
  1. Bulbasaur (dex 1, real single-variety species) -> no "View Alt Forms"
     button/view attached.
  2. Wormadam (dex 413, real 3-variety species: plant/sandy/trash) -> full
     page 1 (default) + Prev/Next-derived pages 2-3.
  3. Minior (dex 774, real 14-variety species) -> capped/sampled at
     FORMS_CAP=12 out of the real 14, with the closing "more forms exist"
     pager page.
  4. Charizard (dex 6, real species with mega-x/mega-y/gmax varieties
     present) -> confirms those battle-only varieties are filtered out of
     `get_species_forms`'s real output, satisfying the card's
     Mega/Gigantamax/Totem/Eternamax-absence acceptance criterion against
     real data (none of cases 1-3's species naturally carry such variants).

Run: `uv run python scripts/proof_multiform_forms_real.py <output_dir>`
"""

from __future__ import annotations

import asyncio
import io
import os
import random
import sys
import tempfile
from datetime import date

import discord
import httpx
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pokesketch import daily, db, pokeapi, ui  # noqa: E402
from pokesketch.embeds import daily_embed  # noqa: E402

FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")

CARD_W = 460
IMAGE_BOX = 340
PAD = 18


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


def _fetch_image(url: str | None) -> Image.Image | None:
    if not url:
        return None
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content)).convert("RGBA")
        return img
    except Exception as exc:  # noqa: BLE001 - proof rendering must not crash on a flaky fetch
        print(f"  [warn] could not fetch {url}: {exc}")
        return None


def render_embed(embed: discord.Embed, *, page_note: str | None = None) -> Image.Image:
    """Render the actual field data of a real `discord.Embed` (title,
    description, image, thumbnail, footer, color) produced by the real
    production embed-builders -- since discord.py cannot render an embed to
    pixels without a live gateway connection (see card constraints), this
    draws the literal Embed object's fields, backed by real fetched images."""
    color = embed.colour.value if embed.colour else 0x5865F2
    color_rgb = ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)

    title_font = _font(FONT_BOLD, 20)
    body_font = _font(FONT_REGULAR, 15)
    footer_font = _font(FONT_REGULAR, 12)
    note_font = _font(FONT_BOLD, 13)

    main_img = _fetch_image(embed.image.url if embed.image else None)
    thumb_img = _fetch_image(embed.thumbnail.url if embed.thumbnail else None)

    scratch = Image.new("RGB", (10, 10))
    scratch_draw = ImageDraw.Draw(scratch)
    title_lines = _wrap(scratch_draw, embed.title or "", title_font, CARD_W - 2 * PAD - 10)
    desc_lines = _wrap(scratch_draw, embed.description or "", body_font, CARD_W - 2 * PAD - 10)

    height = PAD
    height += len(title_lines) * 26 + 8
    height += len(desc_lines) * 20 + 10
    if main_img is not None:
        height += IMAGE_BOX + 10
    if embed.footer and embed.footer.text:
        height += 22
    if page_note:
        height += 24
    height += PAD

    card = Image.new("RGB", (CARD_W, max(height, 120)), (35, 39, 42))
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, 6, card.height], fill=color_rgb)

    y = PAD
    for line in title_lines:
        draw.text((PAD + 8, y), line, font=title_font, fill=(255, 255, 255))
        y += 26
    y += 6
    for line in desc_lines:
        draw.text((PAD + 8, y), line, font=body_font, fill=(210, 212, 216))
        y += 20
    y += 6

    if main_img is not None:
        main_img.thumbnail((IMAGE_BOX, IMAGE_BOX))
        px = PAD + 8
        card.paste(main_img, (px, y), main_img if main_img.mode == "RGBA" else None)
        if thumb_img is not None:
            thumb_img.thumbnail((72, 72))
            tx = px + main_img.width - thumb_img.width - 4
            ty = y + main_img.height - thumb_img.height - 4
            card.paste(thumb_img, (tx, ty), thumb_img if thumb_img.mode == "RGBA" else None)
        y += IMAGE_BOX + 10

    if embed.footer and embed.footer.text:
        draw.text((PAD + 8, y), embed.footer.text, font=footer_font, fill=(150, 152, 157))
        y += 22

    if page_note:
        draw.text((PAD + 8, y), page_note, font=note_font, fill=(88, 101, 242))

    return card


def compose_strip(images: list[Image.Image], *, gap: int = 14) -> Image.Image:
    width = sum(im.width for im in images) + gap * (len(images) - 1)
    height = max(im.height for im in images)
    strip = Image.new("RGB", (width, height), (24, 26, 27))
    x = 0
    for im in images:
        strip.paste(im, (x, 0))
        x += im.width + gap
    return strip


def compose_grid(images: list[Image.Image], *, per_row: int = 4, gap: int = 14) -> Image.Image:
    rows = [images[i : i + per_row] for i in range(0, len(images), per_row)]
    row_strips = [compose_strip(row, gap=gap) for row in rows]
    grid_w = max(r.width for r in row_strips)
    grid_h = sum(r.height for r in row_strips) + gap * (len(row_strips) - 1)
    grid = Image.new("RGB", (grid_w, grid_h), (24, 26, 27))
    y = 0
    for r in row_strips:
        grid.paste(r, (0, y))
        y += r.height + gap
    return grid


class _FakeClient:
    """Minimal stand-in for discord.Client -- carries the real
    PokeApiClient and the real FormsButtonView, matching how
    `PokeSketchDexBot` exposes `.api` / `.forms_button_view`."""

    def __init__(self, api: pokeapi.PokeApiClient, forms_button_view: ui.FormsButtonView) -> None:
        self.api = api
        self.forms_button_view = forms_button_view


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class _FakeResponse:
    def __init__(self) -> None:
        self.edited: dict | None = None
        self.deferred = False

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False) -> None:
        raise AssertionError("FormsButtonView.view_alt_forms defers, not send_message — unexpected call")

    async def edit_message(self, *, embed=None, view=None) -> None:
        self.edited = {"embed": embed, "view": view}

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = True
        self._deferred_ephemeral = ephemeral


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: dict | None = None

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False) -> None:
        self.sent = {"content": content, "embed": embed, "view": view, "ephemeral": ephemeral}


class _FakeInteraction:
    def __init__(self, *, message_id: int | None, client: _FakeClient) -> None:
        self.message = _FakeMessage(message_id) if message_id is not None else None
        self.client = client
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


async def click_and_page_through(client: _FakeClient, message_id: int) -> list[discord.Embed]:
    """Drives the REAL `ui.FormsButtonView.view_alt_forms` click callback,
    then REAL `ui._FormsPaginatorView` Prev/Next callbacks, collecting every
    page's real Embed in order -- exactly what an ephemeral Discord message
    would show as a user pages through."""
    view = client.forms_button_view
    interaction = _FakeInteraction(message_id=message_id, client=client)
    await view.view_alt_forms.callback(interaction)
    assert interaction.response.deferred is True, "button click did not defer its response"
    assert interaction.followup.sent is not None, "button click produced no ephemeral followup"
    assert interaction.followup.sent["ephemeral"] is True
    paginator: ui._FormsPaginatorView = interaction.followup.sent["view"]
    pages = [interaction.followup.sent["embed"]]

    page_interaction = _FakeInteraction(message_id=None, client=client)
    while not paginator.next_button.disabled:
        await paginator.next_button.callback(page_interaction)
        pages.append(page_interaction.response.edited["embed"])
    return pages


async def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    random.seed(20260929)  # stable FORMS_CAP sample for a reproducible proof artifact

    db_dir = tempfile.mkdtemp()
    db.init_engine(os.path.join(db_dir, "proof.db"))
    await db.create_all()

    forms_button_view = ui.FormsButtonView()
    print("== Functions/classes invoked by this proof (real PokeAPI network, no mocks) ==")
    print(" - pokeapi.PokeApiClient.get_species_forms (real, LIVE network)")
    print(" - daily._forms_view_for_dex (real)")
    print(" - ui.FormsButtonView.view_alt_forms.callback (real)")
    print(" - ui._FormsPaginatorView.next_button/prev_button.callback (real)")
    print(" - ui._build_form_pages (real, exercised indirectly via the click callback)")
    print(" - ui._resolve_species_for_message (real, against a real sqlite DB)")
    print()

    # ---------------- Case 1: Bulbasaur, single-form, no button ----------
    print("== Case 1: Bulbasaur (dex 1) -- LIVE PokeAPI, single-form, no button ==")
    cache_dir = tempfile.mkdtemp()
    api = pokeapi.PokeApiClient(cache_dir=cache_dir)
    client = _FakeClient(api, forms_button_view)

    forms = await api.get_species_forms(1)
    print(f"  get_species_forms(1) -> eligible_total={forms.eligible_total} has_alt_forms={forms.has_alt_forms}")
    assert forms.eligible_total == 1
    assert forms.has_alt_forms is False

    resolved_view = await daily._forms_view_for_dex(client, api, 1)
    print(f"  daily._forms_view_for_dex(...) -> {resolved_view!r} (must be None: no button attached)")
    assert resolved_view is None

    ref = await api.get_pokemon(1)
    images = ref.reference_images(shiny=False)
    embed = daily_embed(ref, False, images)
    card1 = render_embed(embed, page_note='No "View Alt Forms" button attached (1 eligible variety, live data)')
    card1.save(os.path.join(out_dir, "case1_bulbasaur_no_button.png"))
    print(f"  saved case1_bulbasaur_no_button.png ({card1.width}x{card1.height})")
    await api.aclose()
    print()

    # ---------------- Case 2: Wormadam, 3 forms, Prev/Next -----------------
    print("== Case 2: Wormadam (dex 413) -- LIVE PokeAPI, 3 forms, Prev/Next ==")
    cache_dir = tempfile.mkdtemp()
    api = pokeapi.PokeApiClient(cache_dir=cache_dir)
    client = _FakeClient(api, forms_button_view)

    forms = await api.get_species_forms(413)
    print(f"  get_species_forms(413) -> eligible_total={forms.eligible_total} forms={[f.name for f in forms.forms]}")
    assert forms.eligible_total == 3
    assert [f.name for f in forms.forms] == ["wormadam-plant", "wormadam-sandy", "wormadam-trash"]
    assert forms.forms[0].is_default is True

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=1))
        s.add(
            db.DailyPokemon(
                guild_id=1, local_date=date(2026, 9, 29), dex_no=413, name="wormadam-plant",
                is_shiny=False, announce_message_id=413000,
            )
        )
        await s.commit()

    pages = await click_and_page_through(client, 413000)
    print(f"  clicked + paged through {len(pages)} pages (default first, then Prev/Next-derived):")
    for i, e in enumerate(pages, start=1):
        print(f"    page {i}: title={e.title!r} desc={e.description!r} image={e.image.url if e.image else None}")
    assert len(pages) == 3
    assert "Wormadam" in pages[0].title

    rendered = [
        render_embed(e, page_note=f"Page {i}/3 (Prev/Next {'start' if i == 1 else 'via Next'})")
        for i, e in enumerate(pages, start=1)
    ]
    strip = compose_strip(rendered)
    strip.save(os.path.join(out_dir, "case2_wormadam_pages.png"))
    print(f"  saved case2_wormadam_pages.png ({strip.width}x{strip.height})")
    await api.aclose()
    print()

    # ---------------- Case 3: Minior, real 14 varieties, capped at 12 -----
    print("== Case 3: Minior (dex 774) -- LIVE PokeAPI, real 14 varieties, capped at 12 ==")
    cache_dir = tempfile.mkdtemp()
    api = pokeapi.PokeApiClient(cache_dir=cache_dir)
    client = _FakeClient(api, forms_button_view)

    forms = await api.get_species_forms(774)
    print(
        f"  get_species_forms(774) -> eligible_total={forms.eligible_total} "
        f"sampled={len(forms.forms)} more_forms_exist={forms.more_forms_exist}"
    )
    assert forms.eligible_total == 14, f"expected 14 real Minior varieties, got {forms.eligible_total}"
    assert len(forms.forms) == pokeapi.FORMS_CAP == 12
    assert forms.more_forms_exist is True
    assert forms.forms[0].is_default is True

    async with db.session() as s:
        s.add(
            db.DailyPokemon(
                guild_id=1, local_date=date(2026, 9, 30), dex_no=774, name=forms.forms[0].name,
                is_shiny=False, announce_message_id=774000,
            )
        )
        await s.commit()

    pages = await click_and_page_through(client, 774000)
    print(f"  clicked + paged through {len(pages)} pages (12 sampled of 14 real forms + 1 closing note):")
    for i, e in enumerate(pages, start=1):
        print(f"    page {i}: title={e.title!r} desc={e.description!r}")
    assert len(pages) == 13
    assert "more forms" in pages[-1].title.lower()
    assert "12 of 14" in pages[-1].description
    await api.aclose()

    highlights = [
        render_embed(pages[0], page_note="Page 1/13 (default variety, live data)"),
        render_embed(pages[11], page_note="Page 12/13 (last sampled form)"),
        render_embed(pages[12], page_note="Page 13/13 (closing pager note: more forms exist)"),
    ]
    strip = compose_strip(highlights)
    strip.save(os.path.join(out_dir, "case3_minior_capped_highlights.png"))
    print(f"  saved case3_minior_capped_highlights.png ({strip.width}x{strip.height})")

    all_pages_render = [render_embed(e, page_note=f"Page {i}/13") for i, e in enumerate(pages, start=1)]
    grid = compose_grid(all_pages_render, per_row=4)
    grid.save(os.path.join(out_dir, "case3_minior_all_13_pages.png"))
    print(f"  saved case3_minior_all_13_pages.png ({grid.width}x{grid.height})")
    print()

    # ---------------- Case 4: real battle-only filter (Charizard) ---------
    print("== Case 4: Charizard (dex 6) -- LIVE PokeAPI, confirms real mega/gmax varieties are filtered ==")
    cache_dir = tempfile.mkdtemp()
    api = pokeapi.PokeApiClient(cache_dir=cache_dir)

    # Read the raw species payload ourselves (bypassing the cache/filter) to
    # show what real PokeAPI actually returns for this species' varieties,
    # so the "confirmed absent" claim below is checked against ground truth,
    # not just against this feature's own already-filtered output.
    raw_resp = await api._client.get(f"{pokeapi.POKEAPI_BASE}/pokemon-species/6")
    raw_resp.raise_for_status()
    raw_variety_names = [v["pokemon"]["name"] for v in raw_resp.json()["varieties"]]
    print(f"  raw pokemon-species/6 varieties (live, unfiltered): {raw_variety_names}")
    battle_only_present = [n for n in raw_variety_names if pokeapi._is_battle_only_variety(n)]
    assert battle_only_present, "expected Charizard's real data to include mega/gmax varieties for this check"
    print(f"  real battle-only varieties present in source data: {battle_only_present}")

    forms = await api.get_species_forms(6)
    filtered_names = [f.name for f in forms.forms]
    print(f"  get_species_forms(6) -> filtered forms: {filtered_names}")
    for bad in battle_only_present:
        assert bad not in filtered_names, f"{bad} leaked through the battle-only filter"
    print("  confirmed: every real mega/gmax variety is absent from the rendered picker output.")
    await api.aclose()
    print()

    print("All proof assertions passed (100% live PokeAPI data, zero mocked network calls).")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    asyncio.run(main(out))
    print(f"\nOutput dir: {out}")
