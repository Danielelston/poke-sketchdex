"""Unit tests for daily.py's "View Alt Forms" button wiring — Unit 3 of the
Multiform Pokemon Reference Picker (see Design/Multiform Pokemon Reference
Picker Plan.md). Plain sync test functions driving asyncio.run(), matching
tests/test_pokeapi_species_forms.py and tests/test_ui_forms_button_view.py's
style (no pytest-asyncio dependency declared in this repo).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date

from pokesketch import db, pokeapi
from pokesketch.daily import post_daily_for_guild, post_wild_encounter_for_guild
from pokesketch.pokeapi import PokemonRef, SpeciesForms
from pokesketch.ui import FormsButtonView


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


def _species_forms(dex_no: int, count: int) -> SpeciesForms:
    forms = [
        pokeapi.SpeciesForm(
            name=f"species-{i}" if i else "species",
            is_default=(i == 0),
            official_artwork=f"https://art.example/{i}.png",
            sprite=f"https://sprite.example/{i}.png",
            shiny_artwork=None,
            shiny_sprite=None,
        )
        for i in range(count)
    ]
    return SpeciesForms(dex_no=dex_no, forms=forms, eligible_total=count)


class _FakeMessage:
    def __init__(self, msg_id: int, thread=None):
        self.id = msg_id
        self._thread = thread

    async def create_thread(self, **kwargs):
        return self._thread


class _FakeThread:
    def __init__(self, thread_id: int):
        self.id = thread_id

    async def send(self, *args, **kwargs):
        return None


class _FakeChannel:
    """Captures every channel.send(...) call's kwargs (in particular `view`)."""

    def __init__(self, channel_id: int, thread_id: int = 555):
        self.id = channel_id
        self.sent_kwargs: list[dict] = []
        self.sent_messages: list[_FakeMessage] = []
        self._thread = _FakeThread(thread_id)

    async def send(self, *args, **kwargs):
        self.sent_kwargs.append(kwargs)
        msg = _FakeMessage(msg_id=10000 + len(self.sent_kwargs), thread=self._thread)
        self.sent_messages.append(msg)
        return msg


class _FakeClient:
    def __init__(self, channel: _FakeChannel, *, forms_button_view=None):
        self._channel = channel
        # Mirrors PokeSketchDexBot's real attribute -- omit it entirely to
        # simulate a client that never registered the persistent view.
        if forms_button_view is not None:
            self.forms_button_view = forms_button_view

    def get_channel(self, cid):
        return self._channel if cid == self._channel.id else None

    async def fetch_channel(self, cid):
        return self._channel


class _StubApi:
    def __init__(self, ref: PokemonRef, forms: SpeciesForms | Exception):
        self._ref = ref
        self._forms = forms

    async def get_pokemon(self, dex_no: int) -> PokemonRef:
        return self._ref

    async def get_species_forms(self, dex_no: int) -> SpeciesForms:
        if isinstance(self._forms, BaseException):
            raise self._forms
        return self._forms


def _fake_ref(dex_no: int) -> PokemonRef:
    return PokemonRef(
        dex_no=dex_no, name=f"fakemon{dex_no}", types=["normal"],
        official_artwork="https://example.invalid/art.png",
        sprite="https://example.invalid/sprite.png",
        shiny_artwork="https://example.invalid/shiny-art.png",
        shiny_sprite="https://example.invalid/shiny-sprite.png",
    )


def test_daily_post_single_form_species_has_no_button():
    async def scenario():
        await _fresh_db()
        guild_id = 1
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=42, dex_min=1, dex_max=1, selection_mode="random"))
            await s.commit()

        channel = _FakeChannel(channel_id=42)
        real_view = FormsButtonView()
        client = _FakeClient(channel, forms_button_view=real_view)
        api = _StubApi(_fake_ref(1), _species_forms(1, count=1))

        posted = await post_daily_for_guild(client, api, guild_id, date(2026, 9, 25))
        assert posted is True
        assert len(channel.sent_kwargs) == 1
        assert channel.sent_kwargs[0]["view"] is None

        from sqlalchemy import select

        async with db.session() as s:
            daily = (await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == guild_id))).scalar_one()
        assert daily.announce_message_id == channel.sent_messages[0].id

        await db.dispose()

    _run(scenario())


def test_daily_post_multi_form_species_attaches_persistent_view():
    async def scenario():
        await _fresh_db()
        guild_id = 2
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=42, dex_min=1, dex_max=1, selection_mode="random"))
            await s.commit()

        channel = _FakeChannel(channel_id=42)
        real_view = FormsButtonView()
        client = _FakeClient(channel, forms_button_view=real_view)
        api = _StubApi(_fake_ref(1), _species_forms(1, count=3))

        posted = await post_daily_for_guild(client, api, guild_id, date(2026, 9, 25))
        assert posted is True
        assert len(channel.sent_kwargs) == 1
        assert channel.sent_kwargs[0]["view"] is real_view

        await db.dispose()

    _run(scenario())


def test_daily_post_forms_lookup_failure_still_posts_without_button():
    async def scenario():
        await _fresh_db()
        guild_id = 3
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=42, dex_min=1, dex_max=1, selection_mode="random"))
            await s.commit()

        channel = _FakeChannel(channel_id=42)
        client = _FakeClient(channel, forms_button_view=FormsButtonView())
        api = _StubApi(_fake_ref(1), RuntimeError("PokeAPI is down"))

        posted = await post_daily_for_guild(client, api, guild_id, date(2026, 9, 25))
        assert posted is True, "a forms-lookup failure must never block the daily post"
        assert len(channel.sent_kwargs) == 1
        assert channel.sent_kwargs[0]["view"] is None

        await db.dispose()

    _run(scenario())


def test_daily_post_no_registered_view_degrades_to_no_button():
    """If the client never registered a persistent view (e.g. a test double,
    or a real client whose setup_hook hasn't run), a multi-form species still
    posts cleanly, just without a button -- getattr(..., None) degrades
    gracefully rather than raising AttributeError."""

    async def scenario():
        await _fresh_db()
        guild_id = 4
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=42, dex_min=1, dex_max=1, selection_mode="random"))
            await s.commit()

        channel = _FakeChannel(channel_id=42)
        client = _FakeClient(channel)  # no forms_button_view attribute at all
        api = _StubApi(_fake_ref(1), _species_forms(1, count=3))

        posted = await post_daily_for_guild(client, api, guild_id, date(2026, 9, 25))
        assert posted is True
        assert channel.sent_kwargs[0]["view"] is None

        await db.dispose()

    _run(scenario())


def test_wild_encounter_multi_form_attaches_view_single_form_does_not():
    async def scenario():
        await _fresh_db()
        guild_id = 5
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=77))
            wv = db.WeeklyVote(guild_id=guild_id, iso_week="2026-W39", category="type", resolved_dex_pool="1")
            s.add(wv)
            await s.commit()

        channel = _FakeChannel(channel_id=77)
        real_view = FormsButtonView()
        client = _FakeClient(channel, forms_button_view=real_view)

        # Multi-form species -> button attached.
        api_multi = _StubApi(_fake_ref(1), _species_forms(1, count=2))
        posted = await post_wild_encounter_for_guild(client, api_multi, guild_id, date(2026, 9, 25))
        assert posted is True
        assert channel.sent_kwargs[0]["view"] is real_view

        await db.dispose()

    _run(scenario())


def test_wild_encounter_single_form_has_no_button_and_forms_failure_still_posts():
    async def scenario():
        await _fresh_db()

        # Single-form species -> no button.
        single_guild = 6
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=single_guild, channel_id=78))
            s.add(db.WeeklyVote(guild_id=single_guild, iso_week="2026-W39", category="type", resolved_dex_pool="1"))
            await s.commit()
        channel = _FakeChannel(channel_id=78)
        client = _FakeClient(channel, forms_button_view=FormsButtonView())
        api_single = _StubApi(_fake_ref(1), _species_forms(1, count=1))
        posted = await post_wild_encounter_for_guild(client, api_single, single_guild, date(2026, 9, 25))
        assert posted is True
        assert channel.sent_kwargs[0]["view"] is None

        # Forms-lookup failure -> post still goes out, no button, no exception.
        fail_guild = 7
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=fail_guild, channel_id=79))
            s.add(db.WeeklyVote(guild_id=fail_guild, iso_week="2026-W39", category="type", resolved_dex_pool="1"))
            await s.commit()
        fail_channel = _FakeChannel(channel_id=79)
        fail_client = _FakeClient(fail_channel, forms_button_view=FormsButtonView())
        api_fail = _StubApi(_fake_ref(1), RuntimeError("PokeAPI is down"))
        fail_posted = await post_wild_encounter_for_guild(fail_client, api_fail, fail_guild, date(2026, 9, 25))
        assert fail_posted is True, "a forms-lookup failure must never block the wild-encounter post"
        assert fail_channel.sent_kwargs[0]["view"] is None

        await db.dispose()

    _run(scenario())


def test_shiny_roll_is_not_rerolled_for_the_forms_lookup():
    """The forms lookup takes no shiny parameter at all -- shiny consistency
    is achieved by DailyPokemon.is_shiny (read later at click-time by
    ui._resolve_species_for_message), not by re-deriving shiny here. This
    guards against a future regression that re-rolls SHINY_CHANCE a second
    time when building the forms view."""

    async def scenario():
        await _fresh_db()
        guild_id = 8
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=42, dex_min=1, dex_max=1, selection_mode="random"))
            await s.commit()

        channel = _FakeChannel(channel_id=42)
        client = _FakeClient(channel, forms_button_view=FormsButtonView())
        api = _StubApi(_fake_ref(1), _species_forms(1, count=2))

        import random as random_module

        roll_calls = {"count": 0}
        real_random = random_module.random

        def _counting_random():
            roll_calls["count"] += 1
            return 0.0  # forces shiny=True (SHINY_CHANCE > 0)

        random_module.random = _counting_random
        try:
            posted = await post_daily_for_guild(client, api, guild_id, date(2026, 9, 25))
        finally:
            random_module.random = real_random
        assert posted is True
        # Exactly one shiny roll (random.choice used by pick_dex_no is a
        # separate call, not random.random) -- the forms lookup must not
        # consume a second one.
        assert roll_calls["count"] == 1, roll_calls["count"]

        from sqlalchemy import select

        async with db.session() as s:
            daily = (
                await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == guild_id))
            ).scalar_one()
        assert daily.is_shiny is True
        assert daily.announce_message_id == channel.sent_messages[0].id

        await db.dispose()

    _run(scenario())


def test_dex_no_override_forces_species_and_bypasses_pick_dex_no():
    """/post-now's dex_no option (admin/test escape hatch for reproducing the
    alt-forms picker against a known species) must post the forced dex
    number, not whatever pick_dex_no would otherwise choose."""

    async def scenario():
        await _fresh_db()
        guild_id = 9
        async with db.session() as s:
            # Configured range deliberately excludes 413 -- if the override
            # didn't bypass pick_dex_no, this range would make 413 unreachable.
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=42, dex_min=1, dex_max=1, selection_mode="random"))
            await s.commit()

        channel = _FakeChannel(channel_id=42)
        real_view = FormsButtonView()
        client = _FakeClient(channel, forms_button_view=real_view)
        api = _StubApi(_fake_ref(413), _species_forms(413, count=3))

        posted = await post_daily_for_guild(client, api, guild_id, date(2026, 9, 25), dex_no_override=413)
        assert posted is True
        assert len(channel.sent_kwargs) == 1
        assert channel.sent_kwargs[0]["view"] is real_view  # multi-form -> button attached

        from sqlalchemy import select

        async with db.session() as s:
            daily = (await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == guild_id))).scalar_one()
        assert daily.dex_no == 413

        await db.dispose()

    _run(scenario())


def test_dex_no_override_replaces_an_existing_same_day_post():
    """Unlike the normal path (idempotent: skips a second post the same
    local_date), a dex_no_override re-run must replace the existing row so
    an admin can re-test multiple dex numbers in one day without waiting."""

    async def scenario():
        await _fresh_db()
        guild_id = 10
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, channel_id=42, dex_min=1, dex_max=1, selection_mode="random"))
            await s.commit()

        channel = _FakeChannel(channel_id=42)
        client = _FakeClient(channel, forms_button_view=FormsButtonView())
        local_date = date(2026, 9, 25)

        api_first = _StubApi(_fake_ref(1), _species_forms(1, count=1))
        posted_first = await post_daily_for_guild(client, api_first, guild_id, local_date)
        assert posted_first is True

        # Normal re-run (no override) stays idempotent.
        posted_again = await post_daily_for_guild(client, api_first, guild_id, local_date)
        assert posted_again is False

        api_override = _StubApi(_fake_ref(774), _species_forms(774, count=14))
        posted_override = await post_daily_for_guild(
            client, api_override, guild_id, local_date, dex_no_override=774
        )
        assert posted_override is True
        assert len(channel.sent_kwargs) == 2  # first post + override post

        from sqlalchemy import select

        async with db.session() as s:
            rows = (
                await s.execute(select(db.DailyPokemon).where(db.DailyPokemon.guild_id == guild_id))
            ).scalars().all()
        assert len(rows) == 1, "override must replace, not duplicate, the day's row"
        assert rows[0].dex_no == 774

        await db.dispose()

    _run(scenario())
