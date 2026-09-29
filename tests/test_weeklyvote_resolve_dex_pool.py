"""Regression test: resolve_dex_pool(CATEGORY_TYPE, ...) must strip the
emoji + title-casing from the winning poll-answer text (e.g. "⚙️ Steel")
back down to the bare type name PokeAPI expects ("steel"), the same way
CATEGORY_HABITAT already normalizes its winner text. Previously this was
passed straight through, giving PokeAPI 400s and crashing the whole
resolve-choice-poll job (see weeklyvote._day2_candidates docstring: Type is
the one category where display text and choice_key are NOT equal).
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from pokesketch import db, weeklyvote


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


class _FakeApi:
    def __init__(self):
        self.requested_type_names: list[str] = []

    async def get_type_pokemon_dex_nos(self, type_name: str) -> list[int]:
        self.requested_type_names.append(type_name)
        if type_name != "steel":
            raise ValueError(f"unexpected type_name passed to PokeAPI: {type_name!r}")
        return [81, 82, 208]


def test_resolve_dex_pool_type_strips_emoji_and_lowercases():
    _run(_fresh_db())
    api = _FakeApi()

    async def go():
        async with db.session() as s:
            return await weeklyvote.resolve_dex_pool(s, api, guild_id=1, category="type", choice_key="⚙️ Steel")

    dex_pool = _run(go())
    assert dex_pool == [81, 82, 208]
    assert api.requested_type_names == ["steel"]
