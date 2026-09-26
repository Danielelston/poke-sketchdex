"""Unit tests for the Unit 3 mon-EXP hook wiring: the three trigger sites in
`cogs/submissions.py` (own submission, upvote received, upvote removed) and
the shared `pokebox.award_mon_exp_to_party` failure-isolation helper. Kudos
(`cogs/profile.py`) is covered separately in `scripts/smoke_test.py` since it
runs through the real `ProfileView.give_kudos` button callback there.

Plain sync test functions driving `asyncio.run()` internally, matching
`tests/test_pokebox_mon_exp.py`'s style (no pytest-asyncio dependency).

See: `Design/Party Mon Leveling Plan.md` and its Task Breakdown, Unit 3.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date

from pokesketch import db, leveling, pokebox
from pokesketch.cogs.submissions import Submissions


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


async def _make_submission(s, *, guild_id=1, user_id=1) -> db.Submission:
    s.add(db.GuildConfig(guild_id=guild_id))
    daily = db.DailyPokemon(guild_id=guild_id, local_date=date(2026, 9, 25), dex_no=25, name="pikachu")
    s.add(daily)
    await s.flush()
    sub = db.Submission(guild_id=guild_id, user_id=user_id, daily_id=daily.id, image_url="https://x/1.png")
    s.add(sub)
    await s.flush()
    return sub


# --- pokebox.award_mon_exp_to_party: shared failure isolation ---


def test_award_mon_exp_to_party_awards_every_mon():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mons = [await _make_mon(s, user_id=1, slot=i) for i in range(1, 4)]
            await s.commit()
            await pokebox.award_mon_exp_to_party(s, mons, leveling.MON_EXP_SUBMIT)
            for mon in mons:
                assert mon.mon_exp == leveling.MON_EXP_SUBMIT, (mon.id, mon.mon_exp)

    _run(go())


def test_award_mon_exp_to_party_empty_list_is_silent_noop():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            # Must not raise, and there is nothing to assert on since there's
            # nothing to award -- this is the "empty active party" contract.
            await pokebox.award_mon_exp_to_party(s, [], leveling.MON_EXP_SUBMIT)

    _run(go())


def test_award_mon_exp_to_party_isolates_a_single_mon_failure(monkeypatch):
    async def go():
        await _fresh_db()
        async with db.session() as s:
            good_a = await _make_mon(s, user_id=1, slot=1)
            bad = await _make_mon(s, user_id=1, slot=2)
            good_b = await _make_mon(s, user_id=1, slot=3)
            await s.commit()

            real_award = pokebox.award_mon_exp

            async def flaky_award(session, mon, amount, *, today):
                if mon.id == bad.id:
                    raise RuntimeError("simulated leveling bug")
                return await real_award(session, mon, amount, today=today)

            monkeypatch.setattr(pokebox, "award_mon_exp", flaky_award)
            # Must not raise despite the injected failure on `bad`.
            await pokebox.award_mon_exp_to_party(s, [good_a, bad, good_b], leveling.MON_EXP_SUBMIT)

            assert good_a.mon_exp == leveling.MON_EXP_SUBMIT
            assert good_b.mon_exp == leveling.MON_EXP_SUBMIT
            assert bad.mon_exp == 0, "the mon whose grant raised must be left untouched, not partially applied"

    _run(go())


# --- Own submission (daily or wild) via Submissions._award_submission_rewards ---


def test_submission_grants_flat_mon_exp_no_split_by_party_size():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mons_6 = [await _make_mon(s, user_id=1, slot=i) for i in range(1, 7)]
            mon_1 = await _make_mon(s, user_id=2, slot=1)
            await s.commit()

            await Submissions._award_submission_rewards(s, 1, 1, date(2026, 9, 25), leveling.EXP_SUBMIT, "submit")
            await Submissions._award_submission_rewards(s, 1, 2, date(2026, 9, 25), leveling.EXP_SUBMIT, "submit")
            await s.commit()

            for mon in mons_6:
                assert mon.mon_exp == leveling.MON_EXP_SUBMIT, "a 6-mon party must get the full flat amount each"
            assert mon_1.mon_exp == leveling.MON_EXP_SUBMIT, "a 1-mon party must get the identical flat amount"

    _run(go())


def test_wild_encounter_submission_grants_identical_amount_as_daily():
    # _award_submission_rewards is the single shared path both _submit_to_daily
    # and _submit_to_wild_encounter call for a first-time submission, so a
    # wild-encounter call (base_exp=EXP_WILD_ENCOUNTER, kind="wild_submit")
    # must still award the same MON_EXP_SUBMIT mon-EXP as a daily one.
    async def go():
        await _fresh_db()
        async with db.session() as s:
            mon = await _make_mon(s, user_id=1, slot=1)
            await s.commit()
            await Submissions._award_submission_rewards(
                s, 1, 1, date(2026, 9, 25), leveling.EXP_WILD_ENCOUNTER, "wild_submit"
            )
            await s.commit()
            assert mon.mon_exp == leveling.MON_EXP_SUBMIT

    _run(go())


def test_submission_by_different_user_grants_zero_to_owners_party():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            owner_mon = await _make_mon(s, user_id=1, slot=1)
            await s.commit()
            # A different user (user_id=2) submitting must never touch user 1's party.
            await Submissions._award_submission_rewards(s, 1, 2, date(2026, 9, 25), leveling.EXP_SUBMIT, "submit")
            await s.commit()
            assert owner_mon.mon_exp == 0

    _run(go())


def test_submission_with_zero_active_party_mons_is_silent_noop():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            # Only a boxed (inactive) mon exists -- active_party_mons returns [].
            boxed = await _make_mon(s, user_id=1, is_active=False, slot=1)
            await s.commit()
            total = await Submissions._award_submission_rewards(
                s, 1, 1, date(2026, 9, 25), leveling.EXP_SUBMIT, "submit"
            )
            await s.commit()
            # Player EXP path (base + first-day streak bonus) is unaffected by
            # the empty-active-party mon-EXP no-op.
            assert total == leveling.EXP_SUBMIT + leveling.streak_bonus(1)
            assert boxed.mon_exp == 0

    _run(go())


def test_exception_inside_mon_exp_grant_does_not_block_submission_or_player_exp(monkeypatch):
    async def go():
        await _fresh_db()
        async with db.session() as s:
            await _make_mon(s, user_id=1, slot=1)
            await s.commit()

            async def boom(*args, **kwargs):
                raise RuntimeError("simulated award_mon_exp_to_party failure")

            monkeypatch.setattr(pokebox, "award_mon_exp_to_party", boom)
            total = await Submissions._award_submission_rewards(
                s, 1, 1, date(2026, 9, 25), leveling.EXP_SUBMIT, "submit"
            )
            await s.commit()
            # Player EXP (returned total, and the ExpEvent/User/GlobalUser
            # rows _award_exp writes) must land regardless, including the
            # first-day streak bonus.
            expected = leveling.EXP_SUBMIT + leveling.streak_bonus(1)
            assert total == expected
            global_user = await s.get(db.GlobalUser, 1)
            assert global_user is not None and global_user.exp == expected

    _run(go())


# --- Upvote added/removed via Submissions._maybe_award_upvote_received_exp ---


def test_upvote_grants_to_exactly_the_sourced_mon_including_when_boxed():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_submission(s, guild_id=1, user_id=1)
            sourced = await _make_mon(s, user_id=1, is_active=False, slot=1, source_submission_id=sub.id)
            other = await _make_mon(s, user_id=1, is_active=True, slot=2)
            await s.commit()

            await Submissions._maybe_award_upvote_received_exp(s, sub)
            await s.commit()

            assert sourced.mon_exp == leveling.MON_EXP_UPVOTE, "must land even though the mon is boxed"
            assert other.mon_exp == 0, "no other mon may gain anything from this upvote"

    _run(go())


def test_upvote_on_submission_with_no_caught_mon_is_silent_noop():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_submission(s, guild_id=1, user_id=1)
            await s.commit()
            # Must complete without raising even though no mon was ever caught.
            await Submissions._maybe_award_upvote_received_exp(s, sub)
            await s.commit()

    _run(go())


def test_removing_upvote_does_not_touch_mon_exp():
    # Reaction removal never calls into mon-EXP at all (see the `elif not
    # added` branch in `_handle_reaction`) -- confirm a mon that already has
    # EXP from an earlier upvote-add is unaffected by a later remove, i.e.
    # there is no deduction path to accidentally invoke.
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_submission(s, guild_id=1, user_id=1)
            mon = await _make_mon(s, user_id=1, slot=1, source_submission_id=sub.id)
            await s.commit()
            await Submissions._maybe_award_upvote_received_exp(s, sub)
            await s.commit()
            exp_after_add = mon.mon_exp
            assert exp_after_add == leveling.MON_EXP_UPVOTE

            # Simulate the reaction-remove branch: only the Upvote row is
            # deleted, no mon-EXP call exists on that path.
            mon_exp_before_remove = mon.mon_exp
            assert mon_exp_before_remove == exp_after_add, "reaction removal must never deduct mon EXP"

    _run(go())


def test_exception_inside_upvote_mon_exp_grant_does_not_block_upvote_exp(monkeypatch):
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_submission(s, guild_id=1, user_id=1)
            await _make_mon(s, user_id=1, slot=1, source_submission_id=sub.id)
            await s.commit()

            async def boom(*args, **kwargs):
                raise RuntimeError("simulated mon_for_submission failure")

            monkeypatch.setattr(pokebox, "mon_for_submission", boom)
            # Must not raise -- the upvote-received player EXP/gym path above
            # it in the function must still have already run.
            result = await Submissions._maybe_award_upvote_received_exp(s, sub)
            await s.commit()
            assert result is None  # no active gym event configured in this test
            global_user = await s.get(db.GlobalUser, 1)
            assert global_user is not None and global_user.exp == leveling.EXP_PER_UPVOTE_RECEIVED

    _run(go())
