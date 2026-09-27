"""Unit tests for the super-like schema and constants (Card 1/5).

Exercises `SuperLikeWallet`/`SuperLike` model creation, the dual source-FK
shape, the per-source-column unique constraints, and the new `leveling.py`
constants against a real (temp-file) SQLite DB, matching the project's
existing offline-smoke-test style. No Discord code — that lands in later
cards.

See: `Design/Super Likes on Submissions Plan.md`, Decomposition sketch, Unit 1.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from pokesketch import db, leveling


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


async def _ensure_guild(s, guild_id=1) -> None:
    if await s.get(db.GuildConfig, guild_id) is None:
        s.add(db.GuildConfig(guild_id=guild_id))
        await s.flush()


async def _make_daily_submission(s, *, user_id=1) -> db.Submission:
    await _ensure_guild(s)
    daily = db.DailyPokemon(guild_id=1, local_date=date(2026, 9, 26), dex_no=25, name="pikachu")
    s.add(daily)
    await s.flush()
    sub = db.Submission(guild_id=1, user_id=user_id, daily_id=daily.id)
    s.add(sub)
    await s.flush()
    return sub


async def _make_wild_submission(s, *, user_id=1) -> db.WildEncounterSubmission:
    await _ensure_guild(s)
    vote = db.WeeklyVote(guild_id=1, iso_week="2026-W39")
    s.add(vote)
    await s.flush()
    encounter = db.WildEncounter(
        guild_id=1, weekly_vote_id=vote.id, local_date=date(2026, 9, 26), dex_no=25, name="pikachu"
    )
    s.add(encounter)
    await s.flush()
    sub = db.WildEncounterSubmission(guild_id=1, user_id=user_id, wild_encounter_id=encounter.id)
    s.add(sub)
    await s.flush()
    return sub


def test_leveling_super_like_constants():
    assert leveling.EXP_PER_SUPER_LIKE == 15
    assert leveling.MON_EXP_SUPER_LIKE == 150
    # Locked ~10x multiplier over EXP_PER_UPVOTE_RECEIVED that MON_EXP_UPVOTE
    # already uses, applied to EXP_PER_SUPER_LIKE.
    assert leveling.MON_EXP_SUPER_LIKE == leveling.EXP_PER_SUPER_LIKE * (
        leveling.MON_EXP_UPVOTE // leveling.EXP_PER_UPVOTE_RECEIVED
    )


def test_super_like_wallet_lazy_default_balance_is_zero():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            wallet = db.SuperLikeWallet(user_id=42)
            s.add(wallet)
            await s.flush()
            assert wallet.balance == 0

    _run(go())


def test_super_like_dual_source_fk_daily_submission():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, user_id=1)
            like = db.SuperLike(submission_id=sub.id, voter_id=2)
            s.add(like)
            await s.flush()
            assert like.submission_id == sub.id
            assert like.wild_encounter_submission_id is None

    _run(go())


def test_super_like_dual_source_fk_wild_encounter_submission():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_wild_submission(s, user_id=1)
            like = db.SuperLike(wild_encounter_submission_id=sub.id, voter_id=2)
            s.add(like)
            await s.flush()
            assert like.wild_encounter_submission_id == sub.id
            assert like.submission_id is None

    _run(go())


def test_super_like_duplicate_daily_submission_rejected():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, user_id=1)
            s.add(db.SuperLike(submission_id=sub.id, voter_id=2))
            await s.flush()
            s.add(db.SuperLike(submission_id=sub.id, voter_id=2))
            with pytest.raises(IntegrityError):
                await s.flush()

    _run(go())


def test_super_like_duplicate_wild_encounter_submission_rejected():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_wild_submission(s, user_id=1)
            s.add(db.SuperLike(wild_encounter_submission_id=sub.id, voter_id=2))
            await s.flush()
            s.add(db.SuperLike(wild_encounter_submission_id=sub.id, voter_id=2))
            with pytest.raises(IntegrityError):
                await s.flush()

    _run(go())


def test_super_like_different_voters_on_same_daily_submission_allowed():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            sub = await _make_daily_submission(s, user_id=1)
            s.add(db.SuperLike(submission_id=sub.id, voter_id=2))
            s.add(db.SuperLike(submission_id=sub.id, voter_id=3))
            await s.flush()

    _run(go())


def test_super_like_same_voter_on_daily_and_wild_submission_both_allowed():
    """A wild-encounter-only unique constraint must not spuriously block a
    daily super like by the same voter (and vice versa) — the two source
    columns are independently NULL for the other row type, so the two
    per-source unique constraints must not cross-collide."""

    async def go():
        await _fresh_db()
        async with db.session() as s:
            daily_sub = await _make_daily_submission(s, user_id=1)
            wild_sub = await _make_wild_submission(s, user_id=1)
            s.add(db.SuperLike(submission_id=daily_sub.id, voter_id=2))
            s.add(db.SuperLike(wild_encounter_submission_id=wild_sub.id, voter_id=2))
            await s.flush()

    _run(go())


def test_award_mon_exp_reusable_for_super_like_amount():
    """award_mon_exp() takes a plain (mon, amount, today) signature with no
    source-specific branching, so it is reusable as-is for the super-like
    grant path — the spend-flow card (3/5) need only call it with
    MON_EXP_SUPER_LIKE."""

    async def go():
        from datetime import date

        from pokesketch import pokebox

        await _fresh_db()
        async with db.session() as s:
            mon = db.CaughtMon(
                user_id=1, is_active=True, slot=1, dex_no=25, name="pikachu",
                cached_image_path="",
            )
            s.add(mon)
            await s.flush()
            awarded = await pokebox.award_mon_exp(
                s, mon, leveling.MON_EXP_SUPER_LIKE, today=date(2026, 9, 26)
            )
            assert awarded == leveling.MON_EXP_SUPER_LIKE
            assert mon.mon_exp == leveling.MON_EXP_SUPER_LIKE

    _run(go())
