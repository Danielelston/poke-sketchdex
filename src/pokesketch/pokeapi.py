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

log = logging.getLogger(__name__)

POKEAPI_BASE = "https://pokeapi.co/api/v2"
USER_AGENT = "PokeSketchDex-Bot/0.1 (+https://github.com/Danielelston/poke-sketchdex)"


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
        return self.name.replace("-", " ").title()

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
