"""Unit tests for the party-mon EXP award helper and lookups in `pokebox.py`.

Exercises `award_mon_exp`, `active_party_mons`, and `mon_for_submission`
against a real (temp-file) SQLite DB via the async engine, matching the
project's existing offline-smoke-test style. Plain sync test functions that
drive `asyncio.run()` internally, since the repo has no pytest-asyncio
dependency declared.

See: `Design/Party Mon Leveling Plan.md` and its Task Breakdown, Unit 2.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date

from pokesketch import db, leveling, pokebox


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


async def _make_mon(s, *, user_id=1, is_active=True, slot=1, source_submission_id=None,
                     source_wild_encounter_submission_id=None) -> db.CaughtMon:
    mon = db.CaughtMon(
        user_id=user_id,
        is_active=is_active,
        slot=slot,
        dex_no=25,
        name="pikachu",
        cached_image_path="",
        source_submission_id=source_submission_id,
        source_wild_encounter_submission_id=source_wild_encounter_submission_id,
    )
    s.add(mon)
    await s.flush()
    return mon


def test_award_mon_exp_basic_award_and_level_recompute():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mon = await _make_mon(s)
            awarded = await pokebox.award_mon_exp(s, mon, 50, today=date(2026, 9, 25))
            assert awarded == 50
            assert mon.mon_exp == 50
            assert mon.exp_today == 50
            assert mon.last_exp_date == date(2026, 9, 25)
            assert mon.mon_level == leveling.mon_level_for_exp(50)

    _run(go())


def test_award_mon_exp_partial_award_crossing_the_cap():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mon = await _make_mon(s)
            first = await pokebox.award_mon_exp(s, mon, 100, today=date(2026, 9, 25))
            assert first == 100
            # Second grant would push 100 + 100 = 200 > 150 cap; only 50 lands.
            second = await pokebox.award_mon_exp(s, mon, 100, today=date(2026, 9, 25))
            assert second == 50
            assert mon.exp_today == leveling.MON_EXP_DAILY_CAP
            assert mon.mon_exp == 150
            # A third grant the same day is fully exhausted -> zero, never rejected/raised.
            third = await pokebox.award_mon_exp(s, mon, 25, today=date(2026, 9, 25))
            assert third == 0
            assert mon.mon_exp == 150

    _run(go())


def test_award_mon_exp_resets_on_new_utc_day_without_scheduled_job():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mon = await _make_mon(s)
            await pokebox.award_mon_exp(s, mon, 150, today=date(2026, 9, 25))
            assert mon.exp_today == 150
            # New UTC day: exp_today resets to 0 lazily on the next grant, no job involved.
            awarded = await pokebox.award_mon_exp(s, mon, 50, today=date(2026, 9, 26))
            assert awarded == 50
            assert mon.exp_today == 50
            assert mon.mon_exp == 200
            assert mon.last_exp_date == date(2026, 9, 26)

    _run(go())


def test_award_mon_exp_does_not_open_its_own_transaction():
    # award_mon_exp should just mutate the ORM object; nothing commits unless
    # the caller commits its own session.
    async def go():
        await _fresh_db()
        mon_id = None
        async with db.session() as s:
            mon = await _make_mon(s)
            mon_id = mon.id
            await s.commit()

        async with db.session() as s:
            mon = await s.get(db.CaughtMon, mon_id)
            await pokebox.award_mon_exp(s, mon, 50, today=date(2026, 9, 25))
            # Deliberately no commit here.

        async with db.session() as s:
            fresh = await s.get(db.CaughtMon, mon_id)
            assert fresh.mon_exp == 0  # uncommitted award never landed

    _run(go())


def test_active_party_mons_only_active_capped_at_six():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            for slot in range(1, 7):
                await _make_mon(s, user_id=1, is_active=True, slot=slot)
            for slot in range(1, 3):
                await _make_mon(s, user_id=1, is_active=False, slot=slot)
            await _make_mon(s, user_id=2, is_active=True, slot=1)
            await s.commit()

        async with db.session() as s:
            party = await pokebox.active_party_mons(s, 1)
            assert len(party) == 6
            assert all(m.is_active for m in party)
            assert all(m.user_id == 1 for m in party)

    _run(go())


def test_mon_for_submission_daily_path():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mon = await _make_mon(s, source_submission_id=42)
            await s.commit()
            found = await pokebox.mon_for_submission(s, 42, wild=False)
            assert found is not None
            assert found.id == mon.id
            # Wrong flag must not match a daily-sourced mon.
            assert await pokebox.mon_for_submission(s, 42, wild=True) is None

    _run(go())


def test_mon_for_submission_wild_path():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mon = await _make_mon(s, source_wild_encounter_submission_id=7)
            await s.commit()
            found = await pokebox.mon_for_submission(s, 7, wild=True)
            assert found is not None
            assert found.id == mon.id
            # Wrong flag must not match a wild-sourced mon.
            assert await pokebox.mon_for_submission(s, 7, wild=False) is None

    _run(go())


def test_mon_for_submission_returns_none_when_nothing_caught():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            assert await pokebox.mon_for_submission(s, 999, wild=False) is None
            assert await pokebox.mon_for_submission(s, 999, wild=True) is None

    _run(go())
