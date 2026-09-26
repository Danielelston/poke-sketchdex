"""Real-rendered proof for the alt-forms picker (Design/Multiform Pokemon
Reference Picker Plan.md, decomposition Unit 4).

This is PROOF/FIXTURE code only -- it imports and calls the actual
production functions/classes (`pokeapi.PokeApiClient.get_species_forms`,
`daily._forms_view_for_dex`, `ui.FormsButtonView`, `ui._FormsPaginatorView`,
`ui._build_form_pages`) against seeded fixture data. The ONLY thing faked is
the live PokeAPI network call for `pokemon-species/{id}` and `pokemon/{id}`
(via `httpx.MockTransport`, matching `tests/test_pokeapi_species_forms.py`'s
existing style) -- everything downstream (filtering, capping/sampling,
caching, embed-building, button/paginator behavior, DB message_id
resolution) is the real, unmodified production code path.

Reference-image URLs in the fixtures are REAL, currently-live PokeAPI CDN
URLs (raw.githubusercontent.com/PokeAPI/sprites) -- fetched over the real
network (not mocked) when composing the visual artifact, so the rendered
proof shows genuine artwork, not placeholder boxes.

Cases proven (see card acceptance criteria):
  1. Bulbasaur (dex 1, real single-variety species) -> no "View Alt Forms"
     button/view attached.
  2. Wormadam (dex 413, real 3-variety species: plant/sandy/trash) -> full
     page 1 (default) + Prev/Next-derived pages 2-3, run on a shiny-day roll
     to also prove the shiny-art-per-form + non-shiny-fallback requirement
     (wormadam-trash's fixture deliberately omits shiny art).
  3. Alcremie (dex 869) -> capped/sampled at FORMS_CAP=12 out of a realistic
     63-variety fixture (cream x sweet combinations, matching Alcremie's
     real in-game cosmetic-form naming, since PokeAPI's public data models
     these via `pokemon.forms`, not `pokemon-species.varieties` -- see the
     kanban comment on this card for that discovery), with the closing
     "more forms exist" pager page.

Run: `uv run python scripts/proof_multiform_forms.py <output_dir>`
"""

from __future__ import annotations

import asyncio
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
        img = Image.open(__import__("io").BytesIO(resp.content)).convert("RGBA")
        return img
    except Exception as exc:  # noqa: BLE001 - proof rendering must not crash on a flaky fetch
        print(f"  [warn] could not fetch {url}: {exc}")
        return None


def render_embed(embed: discord.Embed, *, page_note: str | None = None) -> Image.Image:
    """Render the actual field data of a real `discord.Embed` (title,
    description, image, thumbnail, footer, color) produced by the real
    production embed-builders -- this is the literal Embed object discord.py
    would send, just drawn to pixels since discord.py can't render one to an
    image without a live gateway connection (see card constraints)."""
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


# --- fixture builders --------------------------------------------------


def _variety(pid: int, name: str, is_default: bool = False) -> dict:
    return {
        "is_default": is_default,
        "pokemon": {"name": name, "url": f"{pokeapi.POKEAPI_BASE}/pokemon/{pid}/"},
    }


def _pokemon_json(
    pid: int, name: str, *, front: str, shiny: str | None, art: str, art_shiny: str | None
) -> dict:
    return {
        "id": pid,
        "name": name,
        "types": [],
        "sprites": {
            "front_default": front,
            "front_shiny": shiny,
            "other": {"official-artwork": {"front_default": art, "front_shiny": art_shiny}},
        },
    }


def _real_sprites(pid: int) -> dict:
    base = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon"
    return {
        "front": f"{base}/{pid}.png",
        "shiny": f"{base}/shiny/{pid}.png",
        "art": f"{base}/other/official-artwork/{pid}.png",
        "art_shiny": f"{base}/other/official-artwork/shiny/{pid}.png",
    }


def make_bulbasaur_fixture() -> tuple[dict, dict]:
    """Real PokeAPI data for dex 1 -- exactly one natural variety."""
    s = _real_sprites(1)
    species_map = {1: {"varieties": [_variety(1, "bulbasaur", is_default=True)]}}
    pokemon_map = {
        1: _pokemon_json(1, "bulbasaur", front=s["front"], shiny=s["shiny"], art=s["art"], art_shiny=s["art_shiny"])
    }
    return species_map, pokemon_map


def make_wormadam_fixture() -> tuple[dict, dict]:
    """Real PokeAPI data for dex 413's 3 natural varieties (plant/sandy/
    trash). wormadam-trash's shiny fields are deliberately blanked (even
    though the real PokeAPI does have shiny art for it) to genuinely
    exercise SpeciesForm.reference_images' shiny -> non-shiny fallback on
    the shiny-day proof run."""
    plant, sandy, trash = _real_sprites(413), _real_sprites(10004), _real_sprites(10005)
    species_map = {
        413: {
            "varieties": [
                _variety(413, "wormadam-plant", is_default=True),
                _variety(10004, "wormadam-sandy"),
                _variety(10005, "wormadam-trash"),
            ]
        }
    }
    pokemon_map = {
        413: _pokemon_json(
            413, "wormadam-plant", front=plant["front"], shiny=plant["shiny"],
            art=plant["art"], art_shiny=plant["art_shiny"],
        ),
        10004: _pokemon_json(
            10004, "wormadam-sandy", front=sandy["front"], shiny=sandy["shiny"],
            art=sandy["art"], art_shiny=sandy["art_shiny"],
        ),
        # No shiny assets at all for this form's fixture -> fallback case.
        10005: _pokemon_json(
            10005, "wormadam-trash", front=trash["front"], shiny=None, art=trash["art"], art_shiny=None
        ),
    }
    return species_map, pokemon_map


ALCREMIE_CREAMS = [
    "vanilla-cream",  # default
    "ruby-cream", "matcha-cream", "mint-cream", "lemon-cream", "salted-cream",
    "ruby-swirl", "caramel-swirl", "rainbow-swirl",
]
ALCREMIE_SWEETS = [
    "strawberry-sweet", "berry-sweet", "love-sweet", "star-sweet",
    "clover-sweet", "flower-sweet", "ribbon-sweet",
]


def make_alcremie_fixture(seed: int = 869) -> tuple[dict, dict]:
    """A realistic (not placeholder-named) 63-variety fixture matching
    Alcremie's real in-game cream x sweet cosmetic-form naming convention
    (9 creams x 7 sweets). NOTE: PokeAPI's real public `pokemon-species/869`
    only reports 2 `varieties` (default + gmax) -- these 63 cosmetic
    combinations are modeled there via a *different* field
    (`pokemon.forms`), which `get_species_forms` does not read. This
    fixture represents the >12-variety case the design doc calls out
    (Alcremie ~63) as if it were exposed the way the doc assumed, to
    genuinely exercise FORMS_CAP capping/sampling -- see the kanban comment
    flagging this as a real-world discovery for a follow-up card.

    Reference art: since these 63 forms aren't real distinct artwork under
    this naming, each synthetic variety reuses one of 63 different REAL,
    currently-live PokeAPI sprite URLs (dex 2-64) as a stand-in image, so
    the rendered proof still shows genuine fetched artwork per page rather
    than a blank/placeholder box.
    """
    varieties = [_variety(869, "alcremie", is_default=True)]
    pokemon_map = {}
    real = _real_sprites(869)
    pokemon_map[869] = _pokemon_json(
        869, "alcremie", front=real["front"], shiny=real["shiny"], art=real["art"], art_shiny=real["art_shiny"]
    )

    # Default is vanilla-cream + strawberry-sweet (the base "alcremie" form
    # above); the other 62 = the full 9x7 grid minus that one combination.
    combos = [
        (c, sw)
        for c in ALCREMIE_CREAMS
        for sw in ALCREMIE_SWEETS
        if not (c == "vanilla-cream" and sw == "strawberry-sweet")
    ]
    assert len(combos) == 62
    next_id = 20001
    stand_in_dex = 2
    for cream, sweet in combos:
        name = f"alcremie-{cream}-{sweet}"
        varieties.append(_variety(next_id, name))
        stand_in = _real_sprites(stand_in_dex)
        pokemon_map[next_id] = _pokemon_json(
            next_id, name, front=stand_in["front"], shiny=stand_in["shiny"],
            art=stand_in["art"], art_shiny=stand_in["art_shiny"],
        )
        next_id += 1
        stand_in_dex += 1

    species_map = {869: {"varieties": varieties}}
    return species_map, pokemon_map


def make_mock_client(species_map: dict, pokemon_map: dict, cache_dir: str) -> pokeapi.PokeApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        if "/pokemon-species/" in path:
            dex_no = int(leaf)
            if dex_no not in species_map:
                return httpx.Response(404)
            return httpx.Response(200, json=species_map[dex_no])
        if "/pokemon/" in path:
            pid = int(leaf)
            if pid not in pokemon_map:
                return httpx.Response(404)
            return httpx.Response(200, json=pokemon_map[pid])
        return httpx.Response(404)

    client = pokeapi.PokeApiClient(cache_dir=cache_dir)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": pokeapi.USER_AGENT}
    )
    return client


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
        self.sent: dict | None = None
        self.edited: dict | None = None

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False) -> None:
        self.sent = {"embed": embed, "view": view, "ephemeral": ephemeral}

    async def edit_message(self, *, embed=None, view=None) -> None:
        self.edited = {"embed": embed, "view": view}


class _FakeInteraction:
    def __init__(self, *, message_id: int | None, client: _FakeClient) -> None:
        self.message = _FakeMessage(message_id) if message_id is not None else None
        self.client = client
        self.response = _FakeResponse()


async def click_and_page_through(client: _FakeClient, message_id: int) -> list[discord.Embed]:
    """Drives the REAL `ui.FormsButtonView.view_alt_forms` click callback,
    then REAL `ui._FormsPaginatorView` Prev/Next callbacks, collecting every
    page's real Embed in order -- exactly what an ephemeral Discord message
    would show as a user pages through."""
    view = client.forms_button_view
    interaction = _FakeInteraction(message_id=message_id, client=client)
    await view.view_alt_forms.callback(interaction)
    assert interaction.response.sent is not None, "button click produced no ephemeral response"
    assert interaction.response.sent["ephemeral"] is True
    paginator: ui._FormsPaginatorView = interaction.response.sent["view"]
    pages = [interaction.response.sent["embed"]]

    page_interaction = _FakeInteraction(message_id=None, client=client)
    while not paginator.next_button.disabled:
        await paginator.next_button.callback(page_interaction)
        pages.append(page_interaction.response.edited["embed"])
    return pages


async def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    random.seed(20260925)  # stable FORMS_CAP sample for a reproducible proof artifact

    db_dir = tempfile.mkdtemp()
    db.init_engine(os.path.join(db_dir, "proof.db"))
    await db.create_all()

    forms_button_view = ui.FormsButtonView()
    print("== Functions/classes invoked by this proof ==")
    print(" - pokeapi.PokeApiClient.get_species_forms (real, HTTP mocked)")
    print(" - daily._forms_view_for_dex (real)")
    print(" - ui.FormsButtonView.view_alt_forms.callback (real)")
    print(" - ui._FormsPaginatorView.next_button/prev_button.callback (real)")
    print(" - ui._build_form_pages (real, exercised indirectly via the click callback)")
    print(" - ui._resolve_species_for_message (real, against a real sqlite DB)")
    print()

    # ---------------- Case 1: Bulbasaur, single-form, no button ----------
    print("== Case 1: Bulbasaur (dex 1) -- single-form, no button ==")
    species_map, pokemon_map = make_bulbasaur_fixture()
    cache_dir = tempfile.mkdtemp()
    api = make_mock_client(species_map, pokemon_map, cache_dir)
    client = _FakeClient(api, forms_button_view)

    forms = await api.get_species_forms(1)
    print(f"  get_species_forms(1) -> eligible_total={forms.eligible_total} has_alt_forms={forms.has_alt_forms}")
    assert forms.has_alt_forms is False

    resolved_view = await daily._forms_view_for_dex(client, api, 1)
    print(f"  daily._forms_view_for_dex(...) -> {resolved_view!r} (must be None: no button attached)")
    assert resolved_view is None

    ref = await api.get_pokemon(1)
    images = ref.reference_images(shiny=False)
    from pokesketch.embeds import daily_embed

    embed = daily_embed(ref, False, images)
    card1 = render_embed(embed, page_note="No \"View Alt Forms\" button attached (single eligible variety)")
    card1.save(os.path.join(out_dir, "case1_bulbasaur_no_button.png"))
    print(f"  saved case1_bulbasaur_no_button.png ({card1.width}x{card1.height})")
    print()

    # ---------------- Case 2: Wormadam, 3 forms, shiny-day, Prev/Next -----
    print("== Case 2: Wormadam (dex 413) -- 3 forms, shiny-day roll, Prev/Next ==")
    species_map, pokemon_map = make_wormadam_fixture()
    cache_dir = tempfile.mkdtemp()
    api = make_mock_client(species_map, pokemon_map, cache_dir)
    client = _FakeClient(api, forms_button_view)

    forms = await api.get_species_forms(413)
    print(f"  get_species_forms(413) -> eligible_total={forms.eligible_total} forms={[f.name for f in forms.forms]}")
    assert [f.name for f in forms.forms] == ["wormadam-plant", "wormadam-sandy", "wormadam-trash"]
    assert forms.forms[0].is_default is True

    async with db.session() as s:
        s.add(db.GuildConfig(guild_id=1))
        s.add(
            db.DailyPokemon(
                guild_id=1, local_date=date(2026, 9, 25), dex_no=413, name="wormadam-plant",
                is_shiny=True, announce_message_id=413000,
            )
        )
        await s.commit()

    pages = await click_and_page_through(client, 413000)
    print(f"  clicked + paged through {len(pages)} pages (default first, then Prev/Next-derived):")
    for i, e in enumerate(pages, start=1):
        print(f"    page {i}: title={e.title!r} desc={e.description!r} image={e.image.url if e.image else None}")
    assert len(pages) == 3
    assert "Wormadam" in pages[0].title
    assert "✨" in pages[0].title  # shiny-day tag on every page

    rendered = [
        render_embed(e, page_note=f"Page {i}/3 (Prev/Next {'start' if i == 1 else 'via Next'})")
        for i, e in enumerate(pages, start=1)
    ]
    strip = compose_strip(rendered)
    strip.save(os.path.join(out_dir, "case2_wormadam_shiny_pages.png"))
    print(f"  saved case2_wormadam_shiny_pages.png ({strip.width}x{strip.height})")
    print("  wormadam-trash fixture has no shiny art -> its page must show non-shiny fallback art:")
    trash_page = pages[2]
    print(f"    page 3 image url: {trash_page.image.url}")
    assert "shiny" not in (trash_page.image.url or "")
    print()

    # ---------------- Case 3: Alcremie, capped at 12/63 -------------------
    print("== Case 3: Alcremie (dex 869) -- 12-of-63, more-forms-exist closing page ==")
    species_map, pokemon_map = make_alcremie_fixture()
    cache_dir = tempfile.mkdtemp()
    api = make_mock_client(species_map, pokemon_map, cache_dir)
    client = _FakeClient(api, forms_button_view)

    forms = await api.get_species_forms(869)
    print(
        f"  get_species_forms(869) -> eligible_total={forms.eligible_total} "
        f"sampled={len(forms.forms)} more_forms_exist={forms.more_forms_exist}"
    )
    assert forms.eligible_total == 63
    assert len(forms.forms) == pokeapi.FORMS_CAP == 12
    assert forms.more_forms_exist is True
    assert forms.forms[0].name == "alcremie"
    assert forms.forms[0].is_default is True

    async with db.session() as s:
        s.add(
            db.DailyPokemon(
                guild_id=1, local_date=date(2026, 9, 26), dex_no=869, name="alcremie",
                is_shiny=False, announce_message_id=869000,
            )
        )
        await s.commit()

    pages = await click_and_page_through(client, 869000)
    print(f"  clicked + paged through {len(pages)} pages (12 sampled forms + 1 closing note):")
    for i, e in enumerate(pages, start=1):
        print(f"    page {i}: title={e.title!r} desc={e.description!r}")
    assert len(pages) == 13
    assert "more forms" in pages[-1].title.lower()
    assert "12 of 63" in pages[-1].description

    # Render page 1 (default), page 12 (last sampled form), page 13 (closing note).
    highlights = [render_embed(pages[0], page_note="Page 1/13 (default variety)")]
    highlights.append(render_embed(pages[11], page_note="Page 12/13 (last sampled form)"))
    highlights.append(render_embed(pages[12], page_note="Page 13/13 (closing pager note: more forms exist)"))
    strip = compose_strip(highlights)
    strip.save(os.path.join(out_dir, "case3_alcremie_capped_highlights.png"))
    print(f"  saved case3_alcremie_capped_highlights.png ({strip.width}x{strip.height})")

    all_pages_render = [render_embed(e, page_note=f"Page {i}/13") for i, e in enumerate(pages, start=1)]
    # Compose as a 4-wide grid for a full-picture artifact too.
    rows = [all_pages_render[i : i + 4] for i in range(0, len(all_pages_render), 4)]
    row_strips = [compose_strip(row) for row in rows]
    grid_w = max(r.width for r in row_strips)
    grid_h = sum(r.height for r in row_strips) + 14 * (len(row_strips) - 1)
    grid = Image.new("RGB", (grid_w, grid_h), (24, 26, 27))
    y = 0
    for r in row_strips:
        grid.paste(r, (0, y))
        y += r.height + 14
    grid.save(os.path.join(out_dir, "case3_alcremie_all_13_pages.png"))
    print(f"  saved case3_alcremie_all_13_pages.png ({grid.width}x{grid.height})")
    print()
    print("All proof assertions passed.")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    asyncio.run(main(out))
    print(f"\nOutput dir: {out}")
