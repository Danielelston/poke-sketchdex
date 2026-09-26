"""PokeAPI client with on-disk JSON + image caching.

Fetches official artwork + a sprite for reference. Caches aggressively to
respect PokeAPI (no API key, but they ask you not to hammer it).
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field

import httpx

from .formatting import species_display_name

log = logging.getLogger(__name__)

POKEAPI_BASE = "https://pokeapi.co/api/v2"
USER_AGENT = "PokeSketchDex-Bot/0.1 (+https://github.com/Danielelston/poke-sketchdex)"

# National dex cutoff — PokeAPI's /type and /pokemon-habitat endpoints also
# list megas/regional-forms/etc. with ids far past this, which the daily
# picker and reference-image lookups don't support, so pool resolvers filter
# down to this range.
MAX_NATIONAL_DEX = 1025

# Max natural forms shown per species in the alt-forms picker (default +
# sampled others). See Design/Multiform Pokemon Reference Picker Plan.md.
FORMS_CAP = 12

SPECIES_FORMS_SUBDIR = "species_forms"

# Mega Evolution, Gigantamax, Totem, and Eternamax varieties are temporary
# battle-only forms, not something a player would normally draw as "today's
# Pokemon" — excluded from the natural-forms picker. PokeAPI has no documented
# boolean flag for this; it's a plain string-suffix check on the variety's
# `pokemon.name` (e.g. "charizard-mega-x", "venusaur-gmax", "raticate-totem-alola",
# "eternatus-eternamax").
BATTLE_ONLY_VARIETY_SUFFIXES = ("-mega-x", "-mega-y", "-mega", "-gmax", "-totem", "-eternamax")


def _extract_id_from_url(url: str) -> int:
    """Pull the trailing numeric id out of a PokeAPI resource URL, e.g.
    'https://pokeapi.co/api/v2/pokemon/25/' -> 25."""
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def _is_battle_only_variety(name: str) -> bool:
    """True for Mega/Gigantamax/Totem/Eternamax varieties (see
    BATTLE_ONLY_VARIETY_SUFFIXES) — excluded from the natural-forms picker."""
    return any(name.endswith(suffix) for suffix in BATTLE_ONLY_VARIETY_SUFFIXES)


def _resolve_reference_images(
    official_artwork: str | None,
    sprite: str | None,
    shiny_artwork: str | None,
    shiny_sprite: str | None,
    shiny: bool,
) -> list[str]:
    """Shared 1-2-URL (official art first, then sprite) resolution used by both
    `PokemonRef.reference_images` and `SpeciesForm.reference_images`, including
    the shiny -> non-shiny fallback when a shiny asset is missing."""
    if shiny:
        primary, secondary = shiny_artwork, shiny_sprite
    else:
        primary, secondary = official_artwork, sprite
    out = [u for u in (primary, secondary) if u]
    if not out:
        out = [u for u in (official_artwork, sprite) if u]
    return out[:2]


@dataclass
class PokemonRef:
    dex_no: int
    name: str
    types: list[str]
    official_artwork: str | None
    sprite: str | None
    shiny_artwork: str | None
    shiny_sprite: str | None

    def display_name(self) -> str:
        return species_display_name(self.name)

    def reference_images(self, shiny: bool = False) -> list[str]:
        """Return 1-2 reference image URLs (official art first, then sprite)."""
        return _resolve_reference_images(
            self.official_artwork, self.sprite, self.shiny_artwork, self.shiny_sprite, shiny
        )


@dataclass
class SpeciesForm:
    """One eligible natural variety of a species (e.g. a regional/gender/cosplay
    form) — never a Mega/Gigantamax/Totem/Eternamax variety."""

    name: str
    is_default: bool
    official_artwork: str | None
    sprite: str | None
    shiny_artwork: str | None
    shiny_sprite: str | None

    def display_name(self) -> str:
        return species_display_name(self.name)

    def reference_images(self, shiny: bool = False) -> list[str]:
        return _resolve_reference_images(
            self.official_artwork, self.sprite, self.shiny_artwork, self.shiny_sprite, shiny
        )


@dataclass
class SpeciesForms:
    """A species' eligible natural forms, default-variety first, capped/sampled
    at FORMS_CAP. Built once per species and disk-cached forever — see
    `PokeApiClient.get_species_forms`."""

    dex_no: int
    forms: list[SpeciesForm] = field(default_factory=list)
    # Count of eligible natural varieties BEFORE the FORMS_CAP sample was
    # applied — lets `more_forms_exist` detect a truncated result without the
    # caller re-deriving anything, and lets a fetch failure report "1" (no
    # additional forms) without pretending it inspected any real data.
    eligible_total: int = 1

    @property
    def has_alt_forms(self) -> bool:
        """True when the caller should offer a forms picker at all (2+
        eligible natural varieties) — cheap single-form/multi-form check with
        no re-derivation of the count."""
        return self.eligible_total > 1

    @property
    def more_forms_exist(self) -> bool:
        """True when eligible_total exceeded FORMS_CAP and `forms` is a
        sample, not the full set — surface this as a closing pager note."""
        return self.eligible_total > len(self.forms)


class PokeApiClient:
    def __init__(self, cache_dir: str, timeout: float = 15.0) -> None:
        self.cache_dir = cache_dir
        self.json_dir = os.path.join(cache_dir, "json")
        os.makedirs(self.json_dir, exist_ok=True)
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": USER_AGENT}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _cache_path(self, dex_no: int) -> str:
        return os.path.join(self.json_dir, f"{dex_no}.json")

    async def _get_json(self, endpoint: str, subdir: str, cache_key: str) -> dict:
        """Generic disk-cached GET against `{POKEAPI_BASE}/{endpoint}` used by the
        weekly-vote pool resolvers (type/habitat/location-area lookups) — same
        cache-forever pattern as `_fetch_raw`, since PokeAPI's reference data
        doesn't change underneath a cached response.
        """
        cache_dir = os.path.join(self.cache_dir, subdir)
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{cache_key}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        url = f"{POKEAPI_BASE}/{endpoint}"
        log.info("Fetching PokeAPI: %s", url)
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return data

    async def get_type_pokemon_dex_nos(self, type_name: str) -> list[int]:
        """Dex numbers of every Pokemon with the given type, via `/type/{name}`."""
        data = await self._get_json(f"type/{type_name}", "types", type_name)
        dex_nos = (_extract_id_from_url(entry["pokemon"]["url"]) for entry in data.get("pokemon", []))
        return sorted({d for d in dex_nos if d <= MAX_NATIONAL_DEX})

    async def get_habitat_pokemon_dex_nos(self, habitat_name: str) -> list[int]:
        """Dex numbers of every species with the given habitat, via `/pokemon-habitat/{name}`."""
        data = await self._get_json(f"pokemon-habitat/{habitat_name}", "habitats", habitat_name)
        dex_nos = (_extract_id_from_url(entry["url"]) for entry in data.get("pokemon_species", []))
        return sorted({d for d in dex_nos if d <= MAX_NATIONAL_DEX})

    async def get_location_area_encounters(self, area_slug: str) -> list[int]:
        """Dex numbers encountered in a location-area, via `/location-area/{name}`.

        Gen 9 (Paldea) location-areas are thin/inconsistently named on PokeAPI
        (see pokeapi/pokeapi#958) — a 404 or an empty `pokemon_encounters` list
        both surface here as an empty result rather than an exception, so
        callers can just treat "no pool" as "skip this area."
        """
        try:
            data = await self._get_json(f"location-area/{area_slug}", "location_areas", area_slug)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "Location-area %s fetch failed (%s) — possible Gen9 PokeAPI coverage gap; skipping.",
                area_slug, exc.response.status_code,
            )
            return []
        dex_nos = (
            _extract_id_from_url(entry["pokemon"]["url"]) for entry in data.get("pokemon_encounters", [])
        )
        return sorted({d for d in dex_nos if d <= MAX_NATIONAL_DEX})

    async def _fetch_raw(self, dex_no: int) -> dict:
        path = self._cache_path(dex_no)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        url = f"{POKEAPI_BASE}/pokemon/{dex_no}"
        log.info("Fetching PokeAPI: %s", url)
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        # Store a trimmed subset to keep the cache small.
        trimmed = {
            "id": data["id"],
            "name": data["name"],
            "types": [t["type"]["name"] for t in data.get("types", [])],
            "sprites": data.get("sprites", {}),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(trimmed, fh)
        return trimmed

    def _sprite_cache_path(self, dex_no: int, shiny: bool) -> str:
        sprites_dir = os.path.join(self.cache_dir, "sprites")
        os.makedirs(sprites_dir, exist_ok=True)
        suffix = "_shiny" if shiny else ""
        return os.path.join(sprites_dir, f"{dex_no}{suffix}.png")

    async def get_sprite_image_path(self, dex_no: int, shiny: bool = False) -> str | None:
        """Fetch (once) + cache-to-disk-forever the official-artwork/sprite PNG
        for a species — used to composite party thumbnails (e.g. the profile
        card). Same cache-forever posture as `_fetch_raw`'s JSON cache, just
        for the binary image. Returns None if PokeAPI has no image for this
        species/shiny combination.
        """
        path = self._sprite_cache_path(dex_no, shiny)
        if os.path.exists(path):
            return path
        ref = await self.get_pokemon(dex_no)
        urls = ref.reference_images(shiny=shiny)
        if not urls:
            return None
        log.info("Fetching sprite image: %s", urls[0])
        resp = await self._client.get(urls[0])
        resp.raise_for_status()
        with open(path, "wb") as fh:
            fh.write(resp.content)
        return path

    def _species_forms_cache_path(self, dex_no: int) -> str:
        cache_dir = os.path.join(self.cache_dir, SPECIES_FORMS_SUBDIR)
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{dex_no}.json")

    async def _fetch_species_forms_raw(self, dex_no: int) -> dict:
        """Fetch a species' varieties once, filter out battle-only forms,
        cap/sample at FORMS_CAP (default variety always kept), then fetch each
        kept variety's own sprite data. Raises on any network/schema problem
        so the caller can degrade to single-form behavior."""
        url = f"{POKEAPI_BASE}/pokemon-species/{dex_no}"
        log.info("Fetching PokeAPI: %s", url)
        resp = await self._client.get(url)
        resp.raise_for_status()
        species_data = resp.json()
        varieties = species_data["varieties"]
        eligible = [v for v in varieties if not _is_battle_only_variety(v["pokemon"]["name"])]
        if not eligible:
            raise ValueError(f"no eligible natural varieties for dex {dex_no}")
        eligible_total = len(eligible)

        default = next((v for v in eligible if v.get("is_default")), eligible[0])
        others = [v for v in eligible if v is not default]

        if eligible_total > FORMS_CAP:
            sampled_others = random.sample(others, FORMS_CAP - 1)
        else:
            sampled_others = others
        ordered = [default, *sampled_others]

        forms_out = []
        for variety in ordered:
            variety_id = _extract_id_from_url(variety["pokemon"]["url"])
            pdata = await self._fetch_raw(variety_id)
            sprites = pdata.get("sprites", {})
            other = sprites.get("other", {}) or {}
            official = other.get("official-artwork", {}) or {}
            forms_out.append(
                {
                    "name": pdata.get("name", variety["pokemon"]["name"]),
                    "is_default": bool(variety.get("is_default")),
                    "official_artwork": official.get("front_default"),
                    "sprite": sprites.get("front_default"),
                    "shiny_artwork": official.get("front_shiny"),
                    "shiny_sprite": sprites.get("front_shiny"),
                }
            )
        return {"eligible_total": eligible_total, "forms": forms_out}

    @staticmethod
    def _species_forms_from_dict(dex_no: int, data: dict) -> SpeciesForms:
        forms = [
            SpeciesForm(
                name=f["name"],
                is_default=f["is_default"],
                official_artwork=f.get("official_artwork"),
                sprite=f.get("sprite"),
                shiny_artwork=f.get("shiny_artwork"),
                shiny_sprite=f.get("shiny_sprite"),
            )
            for f in data.get("forms", [])
        ]
        return SpeciesForms(dex_no=dex_no, forms=forms, eligible_total=data.get("eligible_total", len(forms) or 1))

    async def get_species_forms(self, dex_no: int) -> SpeciesForms:
        """A species' eligible natural forms (regional/gender/cosplay/etc.),
        excluding Mega/Gigantamax/Totem/Eternamax varieties, capped/sampled at
        FORMS_CAP and disk-cached forever keyed by species dex number — see
        Design/Multiform Pokemon Reference Picker Plan.md. A fetch failure
        (network error, unexpected schema, Gen 9 coverage gaps as already seen
        in `get_location_area_encounters`) degrades to a single-form result
        rather than raising, so callers (daily/wild-encounter posts) are never
        blocked or delayed by a forms lookup.
        """
        path = self._species_forms_cache_path(dex_no)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return self._species_forms_from_dict(dex_no, json.load(fh))

        try:
            data = await self._fetch_species_forms_raw(dex_no)
        except (httpx.HTTPError, KeyError, TypeError, IndexError, ValueError) as exc:
            log.warning("Species-forms fetch failed for dex %s (%s); treating as single-form.", dex_no, exc)
            return SpeciesForms(dex_no=dex_no, forms=[], eligible_total=1)

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return self._species_forms_from_dict(dex_no, data)

    async def get_pokemon(self, dex_no: int) -> PokemonRef:
        data = await self._fetch_raw(dex_no)
        sprites = data.get("sprites", {})
        other = sprites.get("other", {}) or {}
        official = (other.get("official-artwork", {}) or {})
        return PokemonRef(
            dex_no=data["id"],
            name=data["name"],
            types=data.get("types", []),
            official_artwork=official.get("front_default"),
            sprite=sprites.get("front_default"),
            shiny_artwork=official.get("front_shiny"),
            shiny_sprite=sprites.get("front_shiny"),
        )
