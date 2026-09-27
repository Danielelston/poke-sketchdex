"""Unit tests for Card 2/5 (Earn hook): +1 SuperLikeWallet.balance wired into
the existing /submit accept path for both daily and wild-encounter submission
types, with lazy wallet creation mirroring `_get_or_create_user`.

Plain sync test functions driving asyncio.run(), matching this project's
existing offline-smoke-test style (no pytest-asyncio dependency declared).

See: `Design/Super Likes on Submissions Plan.md`, Decomposition sketch, Unit 2.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date

from pokesketch import db, pokebox
from pokesketch.cogs.submissions import Submissions


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


class _FakeAttachment:
    """Stands in for discord.Attachment -- only .to_file() is ever awaited."""

    async def to_file(self):
        return None


class _FakeSentAttachment:
    def __init__(self, url: str):
        self.url = url


class _FakeMessage:
    def __init__(self, msg_id: int):
        self.id = msg_id
        self.attachments = [_FakeSentAttachment(f"https://x/{msg_id}.png")]

    async def add_reaction(self, emoji):
        return None


class _FakeChannel:
    def __init__(self, channel_id: int = 1):
        self.id = channel_id
        self._next_msg_id = 100

    async def send(self, *args, **kwargs):
        self._next_msg_id += 1
        return _FakeMessage(self._next_msg_id)


async def _ensure_guild(s, guild_id: int = 1) -> None:
    if await s.get(db.GuildConfig, guild_id) is None:
        s.add(db.GuildConfig(guild_id=guild_id))
        await s.flush()


async def _make_daily(s, *, guild_id: int = 1, local_date=date(2026, 9, 26)) -> db.DailyPokemon:
    await _ensure_guild(s, guild_id)
    daily = db.DailyPokemon(guild_id=guild_id, local_date=local_date, dex_no=25, name="pikachu")
    s.add(daily)
    await s.flush()
    return daily


async def _make_wild_encounter(s, *, guild_id: int = 1, local_date=date(2026, 9, 26)) -> db.WildEncounter:
    await _ensure_guild(s, guild_id)
    vote = db.WeeklyVote(guild_id=guild_id, iso_week="2026-W39")
    s.add(vote)
    await s.flush()
    encounter = db.WildEncounter(
        guild_id=guild_id, weekly_vote_id=vote.id, local_date=local_date, dex_no=25, name="pikachu"
    )
    s.add(encounter)
    await s.flush()
    return encounter


# --- Brand-new player has 0 until their first accepted submission ---


def test_brand_new_player_has_zero_super_like_balance():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            wallet = await s.get(db.SuperLikeWallet, 1)
            assert wallet is None

    _run(go())


# --- Daily /submit accept path grants exactly +1 ---


def test_accepted_daily_submission_grants_one_super_like():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            daily = await _make_daily(s)
            channel = _FakeChannel()
            await Submissions._submit_to_daily(
                Submissions.__new__(Submissions), s, daily, channel, _FakeAttachment(), 1, 42, "<@42>"
            )
            await s.commit()

            wallet = await s.get(db.SuperLikeWallet, 42)
            assert wallet is not None
            assert wallet.balance == 1

    _run(go())


def test_accepted_wild_encounter_submission_grants_one_super_like():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            encounter = await _make_wild_encounter(s)
            channel = _FakeChannel()
            await Submissions._submit_to_wild_encounter(
                Submissions.__new__(Submissions), s, encounter, channel, _FakeAttachment(),
                1, 42, "<@42>", encounter.local_date,
            )
            await s.commit()

            wallet = await s.get(db.SuperLikeWallet, 42)
            assert wallet is not None
            assert wallet.balance == 1

    _run(go())


# --- Grant is unconditional: same-day resubmission (update) still grants +1 ---


def test_resubmitting_same_daily_still_grants_a_second_super_like():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            daily = await _make_daily(s)
            channel = _FakeChannel()
            self_ = Submissions.__new__(Submissions)
            await Submissions._submit_to_daily(self_, s, daily, channel, _FakeAttachment(), 1, 42, "<@42>")
            await s.commit()
            await Submissions._submit_to_daily(self_, s, daily, channel, _FakeAttachment(), 1, 42, "<@42>")
            await s.commit()

            wallet = await s.get(db.SuperLikeWallet, 42)
            assert wallet.balance == 2, "every accepted submission counts, including a same-day update"

    _run(go())


def test_resubmitting_same_wild_encounter_still_grants_a_second_super_like():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            encounter = await _make_wild_encounter(s)
            channel = _FakeChannel()
            self_ = Submissions.__new__(Submissions)
            await Submissions._submit_to_wild_encounter(
                self_, s, encounter, channel, _FakeAttachment(), 1, 42, "<@42>", encounter.local_date
            )
            await s.commit()
            await Submissions._submit_to_wild_encounter(
                self_, s, encounter, channel, _FakeAttachment(), 1, 42, "<@42>", encounter.local_date
            )
            await s.commit()

            wallet = await s.get(db.SuperLikeWallet, 42)
            assert wallet.balance == 2, "every accepted submission counts, including a same-day update"

    _run(go())


# --- A rejected (outside grace window) daily submission grants nothing ---


def test_submission_outside_grace_window_grants_no_super_like():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            # local_date far enough in the past to be outside the default
            # 7-day grace window relative to "today".
            daily = await _make_daily(s, local_date=date(2020, 1, 1))
            channel = _FakeChannel()
            confirmation, _exp_msg, _gym_result = await Submissions._submit_to_daily(
                Submissions.__new__(Submissions), s, daily, channel, _FakeAttachment(), 1, 42, "<@42>"
            )
            await s.commit()

            assert "outside" in confirmation
            wallet = await s.get(db.SuperLikeWallet, 42)
            assert wallet is None, "a rejected submission must not create a wallet or grant anything"

    _run(go())


# --- Balances are global (not per-guild) and independent per user ---


def test_super_like_balance_is_global_across_guilds():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            daily_guild1 = await _make_daily(s, guild_id=1)
            daily_guild2 = await _make_daily(s, guild_id=2)
            channel = _FakeChannel()
            self_ = Submissions.__new__(Submissions)
            await Submissions._submit_to_daily(self_, s, daily_guild1, channel, _FakeAttachment(), 1, 42, "<@42>")
            await s.commit()
            await Submissions._submit_to_daily(self_, s, daily_guild2, channel, _FakeAttachment(), 2, 42, "<@42>")
            await s.commit()

            wallet = await s.get(db.SuperLikeWallet, 42)
            assert wallet.balance == 2, "one SuperLikeWallet per user, shared across every guild"

    _run(go())


def test_different_authors_get_independent_balances():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            daily = await _make_daily(s)
            channel = _FakeChannel()
            self_ = Submissions.__new__(Submissions)
            await Submissions._submit_to_daily(self_, s, daily, channel, _FakeAttachment(), 1, 42, "<@42>")
            await s.commit()

            other_wallet = await s.get(db.SuperLikeWallet, 99)
            assert other_wallet is None

    _run(go())


# --- pokebox.grant_super_like helper itself (lazy creation, +1 semantics) ---


def test_grant_super_like_creates_wallet_lazily_starting_at_zero_then_one():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            wallet = await pokebox.get_or_create_super_like_wallet(s, 7)
            assert wallet.balance == 0
            await pokebox.grant_super_like(s, 7)
            await s.commit()
            wallet = await s.get(db.SuperLikeWallet, 7)
            assert wallet.balance == 1

    _run(go())


def test_grant_super_like_accumulates_across_calls():
    async def go():
        await _fresh_db()
        async with db.session() as s:
            await pokebox.grant_super_like(s, 7)
            await pokebox.grant_super_like(s, 7)
            await pokebox.grant_super_like(s, 7)
            await s.commit()
            wallet = await s.get(db.SuperLikeWallet, 7)
            assert wallet.balance == 3

    _run(go())
