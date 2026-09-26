"""Unit tests for `ui.FormsButtonView` / `ui._FormsPaginatorView` — the
persistent, ephemeral alt-forms picker button. Plain sync test functions
driving `asyncio.run()` internally, matching `tests/test_pokeapi_species_forms.py`
and `tests/test_hook_wiring_mon_exp.py`'s style (no pytest-asyncio dependency
declared in this repo).

See: `Design/Multiform Pokemon Reference Picker Plan.md`, decomposition Unit 2.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import date

from pokesketch import bot, db, pokeapi, ui


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


def _species_forms(dex_no: int, count: int, *, eligible_total: int | None = None) -> pokeapi.SpeciesForms:
    forms = [
        pokeapi.SpeciesForm(
            name=f"species-{i}" if i else "species",
            is_default=(i == 0),
            official_artwork=f"https://art.example/{i}.png",
            sprite=f"https://sprite.example/{i}.png",
            shiny_artwork=f"https://art.example/{i}-shiny.png",
            shiny_sprite=f"https://sprite.example/{i}-shiny.png",
        )
        for i in range(count)
    ]
    return pokeapi.SpeciesForms(dex_no=dex_no, forms=forms, eligible_total=eligible_total or count)


class _FakeResponse:
    def __init__(self) -> None:
        self.sent: dict | None = None
        self.edited: dict | None = None
        self.deferred = False
        self.deferred_ephemeral: bool | None = None

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False) -> None:
        self.sent = {"embed": embed, "view": view, "ephemeral": ephemeral, "content": content}

    async def edit_message(self, *, embed=None, view=None) -> None:
        self.edited = {"embed": embed, "view": view}

    async def defer(self, *, ephemeral: bool = False) -> None:
        self.deferred = True
        self.deferred_ephemeral = ephemeral


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: dict | None = None

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False) -> None:
        self.sent = {"embed": embed, "view": view, "ephemeral": ephemeral, "content": content}


class _FakeMessage:
    def __init__(self, message_id: int | None) -> None:
        self.id = message_id


class _FakeClient:
    def __init__(self, api) -> None:
        self.api = api


class _FakeInteraction:
    def __init__(self, *, message_id: int | None, api) -> None:
        self.message = _FakeMessage(message_id) if message_id is not None else None
        self.client = _FakeClient(api)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class _FakeApi:
    def __init__(self, forms: pokeapi.SpeciesForms) -> None:
        self._forms = forms
        self.calls: list[int] = []

    async def get_species_forms(self, dex_no: int) -> pokeapi.SpeciesForms:
        self.calls.append(dex_no)
        return self._forms


# --- FormsButtonView: static custom_id + persistence ---


def test_button_custom_id_is_static_and_view_is_persistent():
    view = ui.FormsButtonView()
    assert view.view_alt_forms.custom_id == ui.FORMS_BUTTON_CUSTOM_ID == "view_alt_forms"
    assert view.timeout is None
    assert view.is_persistent()


def test_registered_exactly_once_via_bot_add_view(monkeypatch):
    registered: list[ui.FormsButtonView] = []

    class _FakeBot:
        def add_view(self, view) -> None:
            registered.append(view)

    fake_bot = _FakeBot()
    bot.PokeSketchDexBot._register_persistent_views(fake_bot)

    assert len(registered) == 1
    assert isinstance(registered[0], ui.FormsButtonView)
    assert registered[0].view_alt_forms.custom_id == "view_alt_forms"


# --- message_id -> species resolution ---


def test_resolves_daily_pokemon_by_announce_message_id():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=1))
            s.add(
                db.DailyPokemon(
                    guild_id=1, local_date=date(2026, 9, 25), dex_no=25, name="pikachu",
                    is_shiny=True, announce_message_id=111,
                )
            )
            await s.commit()

        resolved = await ui._resolve_species_for_message(111)
        assert resolved == (25, "pikachu", True)

    _run(go())


def test_resolves_wild_encounter_by_message_id_and_is_never_shiny():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=1))
            wv = db.WeeklyVote(guild_id=1, iso_week="2026-W39")
            s.add(wv)
            await s.flush()
            s.add(
                db.WildEncounter(
                    guild_id=1, weekly_vote_id=wv.id, local_date=date(2026, 9, 25),
                    dex_no=133, name="eevee", message_id=222,
                )
            )
            await s.commit()

        resolved = await ui._resolve_species_for_message(222)
        assert resolved == (133, "eevee", False)

    _run(go())


def test_unresolved_message_id_returns_none_and_logs_warning(caplog):
    async def go():
        await _fresh_db()
        with caplog.at_level(logging.WARNING, logger="pokesketch.ui"):
            resolved = await ui._resolve_species_for_message(999999)
        assert resolved is None

    _run(go())


def test_none_message_id_returns_none_without_a_db_lookup():
    async def go():
        # No db.init_engine() call at all -- if this touched the db it would
        # raise RuntimeError("Engine not initialized...").
        resolved = await ui._resolve_species_for_message(None)
        assert resolved is None

    _run(go())


# --- click handler: ephemeral response, default-first paging, no author restriction ---


def test_click_produces_ephemeral_response_default_variety_first():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=1))
            s.add(
                db.DailyPokemon(
                    guild_id=1, local_date=date(2026, 9, 25), dex_no=479, name="rotom",
                    announce_message_id=333,
                )
            )
            await s.commit()

        forms = _species_forms(479, 3)
        api = _FakeApi(forms)
        interaction = _FakeInteraction(message_id=333, api=api)
        view = ui.FormsButtonView()

        await view.view_alt_forms.callback(interaction)

        assert interaction.response.deferred is True
        assert interaction.response.deferred_ephemeral is True
        assert interaction.followup.sent is not None
        assert interaction.followup.sent["ephemeral"] is True
        assert "Form 1/3" in interaction.followup.sent["embed"].description
        assert forms.forms[0].is_default
        assert api.calls == [479]

        paginator = interaction.followup.sent["view"]
        assert isinstance(paginator, ui._FormsPaginatorView)
        assert paginator.index == 0
        assert paginator.prev_button.disabled is True
        assert paginator.next_button.disabled is False

    _run(go())


def test_prev_next_page_through_all_forms():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=1))
            s.add(
                db.DailyPokemon(
                    guild_id=1, local_date=date(2026, 9, 25), dex_no=479, name="rotom",
                    announce_message_id=333,
                )
            )
            await s.commit()

        forms = _species_forms(479, 3)
        api = _FakeApi(forms)
        interaction = _FakeInteraction(message_id=333, api=api)
        view = ui.FormsButtonView()
        await view.view_alt_forms.callback(interaction)
        paginator = interaction.followup.sent["view"]

        page_interaction = _FakeInteraction(message_id=None, api=api)
        await paginator.next_button.callback(page_interaction)
        assert paginator.index == 1
        assert "Form 2/3" in page_interaction.response.edited["embed"].description

        await paginator.next_button.callback(page_interaction)
        assert paginator.index == 2
        assert paginator.next_button.disabled is True  # last page

        await paginator.prev_button.callback(page_interaction)
        assert paginator.index == 1
        assert paginator.prev_button.disabled is False
        assert paginator.next_button.disabled is False

    _run(go())


def test_two_independent_clicks_get_independent_page_state():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=1))
            s.add(
                db.DailyPokemon(
                    guild_id=1, local_date=date(2026, 9, 25), dex_no=479, name="rotom",
                    announce_message_id=333,
                )
            )
            await s.commit()

        forms = _species_forms(479, 3)
        api = _FakeApi(forms)
        view = ui.FormsButtonView()

        interaction_a = _FakeInteraction(message_id=333, api=api)
        await view.view_alt_forms.callback(interaction_a)
        paginator_a = interaction_a.followup.sent["view"]

        interaction_b = _FakeInteraction(message_id=333, api=api)
        await view.view_alt_forms.callback(interaction_b)
        paginator_b = interaction_b.followup.sent["view"]

        assert paginator_a is not paginator_b

        # User A pages forward twice; user B's paginator must be untouched.
        await paginator_a.next_button.callback(interaction_a)
        await paginator_a.next_button.callback(interaction_a)
        assert paginator_a.index == 2
        assert paginator_b.index == 0

    _run(go())


def test_no_interaction_check_any_user_reaches_callback():
    """FormsButtonView (unlike ConfirmView/PaginatorView) has no
    interaction_check override, so it always defers to the base
    discord.ui.View behavior of allowing any interaction through."""
    assert "interaction_check" not in ui.FormsButtonView.__dict__


def test_click_with_unresolved_message_sends_ephemeral_warning_not_crash(caplog):
    async def go():
        await _fresh_db()
        api = _FakeApi(_species_forms(1, 2))
        interaction = _FakeInteraction(message_id=424242, api=api)
        view = ui.FormsButtonView()

        with caplog.at_level(logging.WARNING, logger="pokesketch.ui"):
            await view.view_alt_forms.callback(interaction)

        assert interaction.response.deferred is True
        assert interaction.followup.sent["ephemeral"] is True
        assert api.calls == []  # never reached the forms lookup
        assert any("424242" in rec.message for rec in caplog.records)

    _run(go())


# --- more_forms_exist closing page ---


def test_more_forms_exist_appends_closing_note_page():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=1))
            s.add(
                db.DailyPokemon(
                    guild_id=1, local_date=date(2026, 9, 25), dex_no=869, name="alcremie",
                    announce_message_id=444,
                )
            )
            await s.commit()

        forms = _species_forms(869, 12, eligible_total=63)
        api = _FakeApi(forms)
        interaction = _FakeInteraction(message_id=444, api=api)
        view = ui.FormsButtonView()
        await view.view_alt_forms.callback(interaction)

        paginator = interaction.followup.sent["view"]
        assert len(paginator.pages) == 13  # 12 sampled forms + 1 closing note
        assert "12 of 63" in paginator.pages[-1].description

    _run(go())
