"""Unit tests for `PokeApiClient.get_species_forms` / the mega/gmax/totem/
eternamax filter, the FORMS_CAP sample-and-cache behavior, and the
shiny-image fallback shared with `PokemonRef`.

Exercises against a mocked PokeAPI (`httpx.MockTransport`) writing to a
temp-dir disk cache, matching the project's existing offline-test style (see
`tests/test_pokebox_mon_exp.py`): plain sync test functions that drive
`asyncio.run()` internally, since the repo has no pytest-asyncio dependency
declared.

See: `Design/Multiform Pokemon Reference Picker Plan.md`, decomposition
Unit 1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

import httpx

from pokesketch import pokeapi


def _run(coro):
    return asyncio.run(coro)


def _variety(pid: int, name: str, is_default: bool = False) -> dict:
    return {
        "is_default": is_default,
        "pokemon": {"name": name, "url": f"{pokeapi.POKEAPI_BASE}/pokemon/{pid}/"},
    }


def _pokemon_json(pid: int, name: str, *, shiny: bool = True) -> dict:
    return {
        "id": pid,
        "name": name,
        "types": [],
        "sprites": {
            "front_default": f"https://img.example/{name}.png",
            "front_shiny": f"https://img.example/{name}-shiny.png" if shiny else None,
            "other": {
                "official-artwork": {
                    "front_default": f"https://art.example/{name}.png",
                    "front_shiny": f"https://art.example/{name}-shiny.png" if shiny else None,
                }
            },
        },
    }


def _make_client(
    species_map: dict[int, dict], pokemon_map: dict[int, dict], requests: list[str]
) -> pokeapi.PokeApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
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

    tmp = tempfile.mkdtemp()
    client = pokeapi.PokeApiClient(cache_dir=tmp)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": pokeapi.USER_AGENT}
    )
    return client


def test_single_natural_variety_reports_no_alt_forms():
    species_map = {1: {"varieties": [_variety(1, "bulbasaur", is_default=True)]}}
    pokemon_map = {1: _pokemon_json(1, "bulbasaur")}
    client = _make_client(species_map, pokemon_map, [])

    result = _run(client.get_species_forms(1))

    assert result.has_alt_forms is False
    assert result.eligible_total == 1
    assert len(result.forms) == 1
    assert result.forms[0].is_default is True


def test_mega_gmax_totem_eternamax_varieties_are_excluded():
    species_map = {
        6: {
            "varieties": [
                _variety(6, "charizard", is_default=True),
                _variety(10034, "charizard-mega-x"),
                _variety(10035, "charizard-mega-y"),
                _variety(10036, "charizard-gmax"),
                _variety(10037, "charizard-totem"),
                _variety(10038, "arceus-eternamax"),
                _variety(10039, "charizard-alola"),  # a real natural variety, kept
            ]
        }
    }
    pokemon_map = {
        pid: _pokemon_json(pid, name)
        for pid, name in [
            (6, "charizard"),
            (10034, "charizard-mega-x"),
            (10035, "charizard-mega-y"),
            (10036, "charizard-gmax"),
            (10037, "charizard-totem"),
            (10038, "arceus-eternamax"),
            (10039, "charizard-alola"),
        ]
    }
    client = _make_client(species_map, pokemon_map, [])

    result = _run(client.get_species_forms(6))

    assert result.eligible_total == 2
    names = {f.name for f in result.forms}
    assert names == {"charizard", "charizard-alola"}
    assert result.forms[0].name == "charizard"  # default first
    assert result.forms[0].is_default is True


def test_at_most_forms_cap_varieties_are_all_included_default_first():
    varieties = [_variety(0, "form-0", is_default=True)] + [_variety(i, f"form-{i}") for i in range(1, 5)]
    species_map = {1: {"varieties": varieties}}
    pokemon_map = {i: _pokemon_json(i, f"form-{i}") for i in range(0, 5)}
    client = _make_client(species_map, pokemon_map, [])

    result = _run(client.get_species_forms(1))

    assert result.eligible_total == 5
    assert len(result.forms) == 5
    assert result.more_forms_exist is False
    assert result.forms[0].name == "form-0"
    assert result.forms[0].is_default is True
    assert {f.name for f in result.forms} == {f"form-{i}" for i in range(0, 5)}


def test_over_forms_cap_samples_stably_and_flags_more_forms_exist():
    total = 20
    varieties = [_variety(0, "form-0", is_default=True)] + [_variety(i, f"form-{i}") for i in range(1, total)]
    species_map = {1: {"varieties": varieties}}
    pokemon_map = {i: _pokemon_json(i, f"form-{i}") for i in range(total)}
    client = _make_client(species_map, pokemon_map, [])

    first = _run(client.get_species_forms(1))
    assert first.eligible_total == total
    assert len(first.forms) == pokeapi.FORMS_CAP
    assert first.more_forms_exist is True
    assert first.forms[0].name == "form-0"
    assert first.forms[0].is_default is True

    # Re-fetching (from cache now) must return the exact same 12 — stable,
    # not re-randomized on every call.
    second = _run(client.get_species_forms(1))
    assert [f.name for f in second.forms] == [f.name for f in first.forms]


def test_shiny_roll_uses_shiny_art_with_non_shiny_fallback():
    shiny_form = pokeapi.SpeciesForm(
        name="pikachu",
        is_default=True,
        official_artwork="https://art.example/pikachu.png",
        sprite="https://img.example/pikachu.png",
        shiny_artwork="https://art.example/pikachu-shiny.png",
        shiny_sprite="https://img.example/pikachu-shiny.png",
    )
    assert shiny_form.reference_images(shiny=True) == [
        "https://art.example/pikachu-shiny.png",
        "https://img.example/pikachu-shiny.png",
    ]

    # No shiny sprite for this form -> fall back to its non-shiny art.
    no_shiny_form = pokeapi.SpeciesForm(
        name="unown-a",
        is_default=False,
        official_artwork="https://art.example/unown-a.png",
        sprite="https://img.example/unown-a.png",
        shiny_artwork=None,
        shiny_sprite=None,
    )
    assert no_shiny_form.reference_images(shiny=True) == [
        "https://art.example/unown-a.png",
        "https://img.example/unown-a.png",
    ]


def test_species_fetch_failure_degrades_to_single_form_and_warns(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    tmp = tempfile.mkdtemp()
    client = pokeapi.PokeApiClient(cache_dir=tmp)
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"User-Agent": pokeapi.USER_AGENT}
    )

    with caplog.at_level(logging.WARNING, logger="pokesketch.pokeapi"):
        result = _run(client.get_species_forms(999))

    assert result.has_alt_forms is False
    assert result.eligible_total == 1
    assert result.forms == []
    assert any("Species-forms fetch failed" in rec.message for rec in caplog.records)


def test_first_fetch_logs_info_and_caches_then_skips_network(caplog):
    species_map = {25: {"varieties": [_variety(25, "pikachu", is_default=True)]}}
    pokemon_map = {25: _pokemon_json(25, "pikachu")}
    requests: list[str] = []
    client = _make_client(species_map, pokemon_map, requests)

    with caplog.at_level(logging.INFO, logger="pokesketch.pokeapi"):
        first = _run(client.get_species_forms(25))
    assert first.eligible_total == 1
    assert any("Fetching PokeAPI" in rec.message for rec in caplog.records)

    cache_path = client._species_forms_cache_path(25)
    assert os.path.exists(cache_path)
    assert cache_path.endswith(os.path.join("species_forms", "25.json"))

    request_count_after_first = len(requests)
    second = _run(client.get_species_forms(25))
    assert len(requests) == request_count_after_first  # no new network calls
    assert second.eligible_total == first.eligible_total
