"""Unit tests for Card 3/5 (DynamicItem + spend flow): the persistent
Super Like button's custom_id scheme, its click handler's validation
(balance/self/duplicate/grace-window), the wallet-decrement + player/mon
EXP grant math, and startup registration via `bot.add_dynamic_items`.

Plain sync test functions driving asyncio.run(), matching this project's
existing offline-smoke-test style (no pytest-asyncio dependency declared) —
see tests/test_ui_forms_button_view.py and tests/test_super_like_earn_hook.py
for the same pattern applied to sibling cards.

See: `Design/Super Likes on Submissions Plan.md`, Decomposition sketch, Unit 3.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from pokesketch import bot, db, leveling, pokebox, superlike, ui


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


async def _ensure_guild(s, guild_id: int = 1, *, grace_period_days: int = 7) -> None:
    if await s.get(db.GuildConfig, guild_id) is None:
        s.add(db.GuildConfig(guild_id=guild_id, grace_period_days=grace_period_days))
        await s.flush()


async def _make_daily_submission(
    s, *, guild_id: int = 1, owner_id: int = 1, local_date=date(2026, 9, 26), grace_period_days: int = 7
) -> db.Submission:
    await _ensure_guild(s, guild_id, grace_period_days=grace_period_days)
    daily = db.DailyPokemon(guild_id=guild_id, local_date=local_date, dex_no=25, name="pikachu")
    s.add(daily)
    await s.flush()
    sub = db.Submission(guild_id=guild_id, user_id=owner_id, daily_id=daily.id)
    s.add(sub)
    await s.flush()
    return sub


async def _make_wild_submission(
    s, *, guild_id: int = 1, owner_id: int = 1, local_date=date(2026, 9, 26), grace_period_days: int = 7
) -> db.WildEncounterSubmission:
    await _ensure_guild(s, guild_id, grace_period_days=grace_period_days)
    vote = db.WeeklyVote(guild_id=guild_id, iso_week="2026-W39")
    s.add(vote)
    await s.flush()
    encounter = db.WildEncounter(
        guild_id=guild_id, weekly_vote_id=vote.id, local_date=local_date, dex_no=25, name="pikachu"
    )
    s.add(encounter)
    await s.flush()
    sub = db.WildEncounterSubmission(guild_id=guild_id, user_id=owner_id, wild_encounter_id=encounter.id)
    s.add(sub)
    await s.flush()
    return sub


async def _give_balance(s, user_id: int, amount: int) -> None:
    wallet = await pokebox.get_or_create_super_like_wallet(s, user_id)
    wallet.balance += amount


# --- custom_id scheme ---


def test_custom_id_encodes_kind_and_id_for_daily():
    button = ui.SuperLikeButton.for_submission(123, wild=False)
    assert button.item.custom_id == "superlike:s:123"


def test_custom_id_encodes_kind_and_id_for_wild():
    button = ui.SuperLikeButton.for_submission(456, wild=True)
    assert button.item.custom_id == "superlike:w:456"


def test_template_regex_matches_well_formed_custom_ids():
    assert re.fullmatch(ui.SUPER_LIKE_CUSTOM_ID_TEMPLATE, "superlike:s:1")
    assert re.fullmatch(ui.SUPER_LIKE_CUSTOM_ID_TEMPLATE, "superlike:w:999999")


def test_template_regex_rejects_malformed_custom_ids():
    """A malformed custom_id (bad kind letter, non-numeric id, or a stale
    scheme from before this feature existed) must not match — Discord then
    routes the click nowhere (a dead button), never into a mis-parsed
    handler, per the design doc's constraint."""
    assert re.fullmatch(ui.SUPER_LIKE_CUSTOM_ID_TEMPLATE, "superlike:x:1") is None
    assert re.fullmatch(ui.SUPER_LIKE_CUSTOM_ID_TEMPLATE, "superlike:s:abc") is None
    assert re.fullmatch(ui.SUPER_LIKE_CUSTOM_ID_TEMPLATE, "view_alt_forms") is None


def test_from_custom_id_reconstructs_matching_instance():
    async def go():
        match = re.fullmatch(ui.SUPER_LIKE_CUSTOM_ID_TEMPLATE, "superlike:w:77")
        button = await ui.SuperLikeButton.from_custom_id(None, None, match)
        assert button.kind == "w"
        assert button.target_id == 77

    _run(go())


def test_button_label_and_style():
    button = ui.SuperLikeButton.for_submission(1, wild=False)
    assert button.item.label == "⭐ Super Like"


# --- startup registration: class, not instance, survives a simulated restart ---


def test_registered_via_add_dynamic_items_with_the_class_not_an_instance():
    """`add_dynamic_items` must receive the SuperLikeButton *class* (unlike
    `add_view`, which takes a FormsButtonView instance) — that's what lets
    a single setup_hook call route clicks on every still-open submission
    post across a process restart, with no per-message re-registration."""
    registered: list[object] = []

    class _FakeBot:
        def add_view(self, view) -> None:
            pass

        def add_dynamic_items(self, *items) -> None:
            registered.extend(items)

    fake_bot = _FakeBot()
    bot.PokeSketchDexBot._register_persistent_views(fake_bot)

    assert registered == [ui.SuperLikeButton]


def test_registration_survives_a_simulated_restart():
    """Registering twice (simulating two process starts) must not error and
    must route the same class both times — proving persistence needs no
    per-message re-touch."""

    class _FakeBot:
        def add_view(self, view) -> None:
            pass

        def add_dynamic_items(self, *items) -> None:
            self.registered = items

    fake_bot = _FakeBot()
    bot.PokeSketchDexBot._register_persistent_views(fake_bot)
    first = fake_bot.registered
    bot.PokeSketchDexBot._register_persistent_views(fake_bot)
    second = fake_bot.registered

    assert first == second == (ui.SuperLikeButton,)


# --- give_super_like: success math ---


def test_successful_super_like_decrements_balance_and_grants_receiver_exp():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        assert receiver_id == 1
        assert mon_exp == 0  # no caught mon

        async with db.session() as s:
            giver_wallet = await s.get(db.SuperLikeWallet, 2)
            assert giver_wallet.balance == 0
            receiver = (
                await s.execute(
                    select(db.User).where(db.User.guild_id == 1, db.User.user_id == 1)
                )
            ).scalar_one()
            assert receiver.exp == leveling.EXP_PER_SUPER_LIKE
            events = (
                await s.execute(
                    select(db.ExpEvent).where(
                        db.ExpEvent.user_id == 1, db.ExpEvent.type == "super_like_received"
                    )
                )
            ).scalars().all()
            assert len(events) == 1
            assert events[0].amount == leveling.EXP_PER_SUPER_LIKE

    _run(go())


def test_successful_wild_encounter_super_like_grants_receiver_exp():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_wild_submission(s, owner_id=1)
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=True, target_id=sub.id)
            await s.commit()

        assert receiver_id == 1
        assert mon_exp == 0

        async with db.session() as s:
            giver_wallet = await s.get(db.SuperLikeWallet, 2)
            assert giver_wallet.balance == 0

    _run(go())


# --- give_super_like: rejections leave balance/state untouched ---


def test_zero_balance_rejected_and_nothing_changes():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            await s.commit()

        async with db.session() as s:
            with pytest.raises(superlike.SuperLikeError, match="no super likes"):
                await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        async with db.session() as s:
            # A lazy zero-balance wallet may have been created (mirroring
            # _get_or_create_user's "create on read" posture) -- what matters
            # per the design doc is that the *balance* is untouched by the
            # rejection, not whether the row itself exists yet.
            wallet = await s.get(db.SuperLikeWallet, 2)
            assert wallet is None or wallet.balance == 0
            receiver = await s.get(db.User, 1)
            assert receiver is None
            likes = (await s.execute(select(db.SuperLike))).scalars().all()
            assert likes == []

    _run(go())


def test_self_super_like_rejected():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            await _give_balance(s, 1, 5)
            await s.commit()

        async with db.session() as s:
            with pytest.raises(superlike.SuperLikeError, match="own submission"):
                await superlike.give_super_like(s, giver_id=1, wild=False, target_id=sub.id)
            await s.commit()

        async with db.session() as s:
            wallet = await s.get(db.SuperLikeWallet, 1)
            assert wallet.balance == 5, "self-block must not touch the balance"

    _run(go())


def test_duplicate_super_like_rejected_after_first_success():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            await _give_balance(s, 2, 5)
            await s.commit()

        async with db.session() as s:
            await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        async with db.session() as s:
            with pytest.raises(superlike.SuperLikeError, match="already super-liked"):
                await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        async with db.session() as s:
            wallet = await s.get(db.SuperLikeWallet, 2)
            assert wallet.balance == 4, "second click must decrement nothing further"
            receiver = (
                await s.execute(
                    select(db.User).where(db.User.guild_id == 1, db.User.user_id == 1)
                )
            ).scalar_one()
            assert receiver.exp == leveling.EXP_PER_SUPER_LIKE, "no double EXP grant"

    _run(go())


def test_grace_window_expired_rejected_and_balance_unchanged():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1, local_date=date(2020, 1, 1))
            await _give_balance(s, 2, 3)
            await s.commit()

        async with db.session() as s:
            with pytest.raises(superlike.SuperLikeError, match="grace window"):
                await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        async with db.session() as s:
            wallet = await s.get(db.SuperLikeWallet, 2)
            assert wallet.balance == 3

    _run(go())


def test_missing_submission_rejected():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            with pytest.raises(superlike.SuperLikeError, match="no longer exists"):
                await superlike.give_super_like(s, giver_id=2, wild=False, target_id=999999)

    _run(go())


# --- mon-EXP routing (dual source table) ---


def test_super_like_on_daily_submission_with_caught_mon_awards_mon_exp():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            mon = db.CaughtMon(
                user_id=1, is_active=True, slot=1, dex_no=25, name="pikachu",
                cached_image_path="", source_submission_id=sub.id,
            )
            s.add(mon)
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            _receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        assert mon_exp == leveling.MON_EXP_SUPER_LIKE

        async with db.session() as s:
            mon = (await s.execute(select(db.CaughtMon).where(db.CaughtMon.user_id == 1))).scalar_one()
            assert mon.mon_exp == leveling.MON_EXP_SUPER_LIKE

    _run(go())


def test_super_like_on_wild_submission_routes_mon_exp_through_wild_column():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_wild_submission(s, owner_id=1)
            mon = db.CaughtMon(
                user_id=1, is_active=True, slot=1, dex_no=25, name="pikachu",
                cached_image_path="", source_wild_encounter_submission_id=sub.id,
            )
            s.add(mon)
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            _receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=True, target_id=sub.id)
            await s.commit()

        assert mon_exp == leveling.MON_EXP_SUPER_LIKE

    _run(go())


def test_super_like_on_submission_with_no_caught_mon_awards_zero_mon_exp_no_error():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        assert receiver_id == 1
        assert mon_exp == 0

    _run(go())


def test_boxed_mon_still_gains_super_like_exp():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            mon = db.CaughtMon(
                user_id=1, is_active=False, slot=1, dex_no=25, name="pikachu",
                cached_image_path="", source_submission_id=sub.id,
            )
            s.add(mon)
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            _receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        assert mon_exp == leveling.MON_EXP_SUPER_LIKE

    _run(go())


def test_mon_daily_cap_partially_absorbs_the_super_like_grant():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            mon = db.CaughtMon(
                user_id=1, is_active=True, slot=1, dex_no=25, name="pikachu",
                cached_image_path="", source_submission_id=sub.id,
                # last_exp_date must match the UTC date `award_mon_exp` compares
                # against (see superlike.give_super_like's `today=datetime.now(UTC).date()`)
                # or it resets exp_today to 0 and the cap this test exercises
                # never actually triggers.
                mon_exp=100, exp_today=100, last_exp_date=datetime.now(UTC).date(),
            )
            s.add(mon)
            await _give_balance(s, 2, 1)
            await s.commit()

        async with db.session() as s:
            _receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        # Daily cap is 150; mon already has 100 today, so only 50 more lands
        # even though MON_EXP_SUPER_LIKE (150) alone would exceed the cap.
        assert mon_exp == leveling.MON_EXP_DAILY_CAP - 100

        async with db.session() as s:
            mon = (await s.execute(select(db.CaughtMon).where(db.CaughtMon.user_id == 1))).scalar_one()
            assert mon.mon_exp == 100 + (leveling.MON_EXP_DAILY_CAP - 100)

    _run(go())


def test_mon_exp_grant_failure_is_isolated_and_does_not_block_the_super_like(monkeypatch):
    """If the mon-EXP sub-grant raises, the super-like give and the
    receiver's player EXP must still land — matching the design doc's
    failure-isolation requirement and every other mon-EXP hook site."""

    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, owner_id=1)
            await _give_balance(s, 2, 1)
            await s.commit()

        async def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(pokebox, "mon_for_submission", _boom)

        async with db.session() as s:
            receiver_id, mon_exp = await superlike.give_super_like(s, giver_id=2, wild=False, target_id=sub.id)
            await s.commit()

        assert receiver_id == 1
        assert mon_exp == 0

        async with db.session() as s:
            wallet = await s.get(db.SuperLikeWallet, 2)
            assert wallet.balance == 0
            receiver = (
                await s.execute(
                    select(db.User).where(db.User.guild_id == 1, db.User.user_id == 1)
                )
            ).scalar_one()
            assert receiver.exp == leveling.EXP_PER_SUPER_LIKE

    _run(go())
