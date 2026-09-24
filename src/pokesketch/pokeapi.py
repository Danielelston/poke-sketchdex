"""PokeAPI client with on-disk JSON + image caching.

Fetches official artwork + a sprite for reference. Caches aggressively to
respect PokeAPI (no API key, but they ask you not to hammer it).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

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


def _extract_id_from_url(url: str) -> int:
    """Pull the trailing numeric id out of a PokeAPI resource URL, e.g.
    'https://pokeapi.co/api/v2/pokemon/25/' -> 25."""
    return int(url.rstrip("/").rsplit("/", 1)[-1])


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
        if shiny:
            primary, secondary = self.shiny_artwork, self.shiny_sprite
        else:
            primary, secondary = self.official_artwork, self.sprite
        out = [u for u in (primary, secondary) if u]
        # Fall back to non-shiny if a shiny asset is missing.
        if not out:
            out = [u for u in (self.official_artwork, self.sprite) if u]
        return out[:2]


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
