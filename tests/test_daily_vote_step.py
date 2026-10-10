"""Tests for the vote step folded into the daily job."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pokesketch import db, weeklyvote
from pokesketch.bot import PokeSketchDexBot


def _make_bot() -> PokeSketchDexBot:
    cfg = MagicMock()
    cfg.db_path = ":memory:"
    cfg.image_cache_dir = "/tmp"
    bot = PokeSketchDexBot(cfg)
    bot.api = MagicMock()
    return bot


@pytest.fixture
def fresh_db(tmp_path):
    db.init_engine(f"sqlite+aiosqlite:///{tmp_path}/test.db")

    async def _setup():
        await db.create_all()

    async def _teardown():
        await db.dispose()

    import asyncio

    asyncio.run(_setup())
    yield
    asyncio.run(_teardown())


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_vote_step_day1_posts_category_poll(fresh_db):
    bot = _make_bot()

    async def setup():
        async with db.session() as s:
            cfg = db.GuildConfig(guild_id=1, timezone="UTC", vote_day1_weekday=6)  # Sunday
            s.add(cfg)
            await s.commit()

    _run(setup())

    target_date = date(2026, 10, 11)  # Sunday
    with patch.object(weeklyvote, "post_category_poll", new=AsyncMock(return_value=True)) as mock_poll:
        with patch("pokesketch.bot.datetime", wraps=__import__("datetime").datetime) as mock_dt:
            mock_dt.now = MagicMock(return_value=MagicMock(date=lambda: target_date))
            _run(bot._run_vote_step(1, "UTC"))
    mock_poll.assert_awaited_once_with(bot, 1)


def test_vote_step_day2_resolves_category_poll(fresh_db):
    bot = _make_bot()

    async def setup():
        async with db.session() as s:
            cfg = db.GuildConfig(guild_id=1, timezone="UTC", vote_day1_weekday=6)
            s.add(cfg)
            await s.commit()

    _run(setup())

    target_date = date(2026, 10, 12)  # Monday
    with patch.object(weeklyvote, "resolve_category_poll", new=AsyncMock(return_value=True)) as mock_resolve:
        with patch("pokesketch.bot.datetime", wraps=__import__("datetime").datetime) as mock_dt:
            mock_dt.now = MagicMock(return_value=MagicMock(date=lambda: target_date))
            _run(bot._run_vote_step(1, "UTC"))
    mock_resolve.assert_awaited_once_with(bot, bot.api, 1)


def test_vote_step_resolve_day_resolves_choice_poll(fresh_db):
    bot = _make_bot()

    async def setup():
        async with db.session() as s:
            cfg = db.GuildConfig(guild_id=1, timezone="UTC", vote_day1_weekday=6)
            s.add(cfg)
            await s.commit()

    _run(setup())

    target_date = date(2026, 10, 13)  # Tuesday
    with patch.object(weeklyvote, "resolve_choice_poll", new=AsyncMock(return_value=True)) as mock_resolve:
        with patch("pokesketch.bot.datetime", wraps=__import__("datetime").datetime) as mock_dt:
            mock_dt.now = MagicMock(return_value=MagicMock(date=lambda: target_date))
            _run(bot._run_vote_step(1, "UTC"))
    mock_resolve.assert_awaited_once_with(bot, bot.api, 1)


def test_vote_step_other_day_no_op(fresh_db):
    bot = _make_bot()

    async def setup():
        async with db.session() as s:
            cfg = db.GuildConfig(guild_id=1, timezone="UTC", vote_day1_weekday=6)
            s.add(cfg)
            await s.commit()

    _run(setup())

    target_date = date(2026, 10, 14)  # Wednesday
    with patch.object(weeklyvote, "post_category_poll", new=AsyncMock()) as mock_cat:
        with patch.object(weeklyvote, "resolve_category_poll", new=AsyncMock()) as mock_res_cat:
            with patch.object(weeklyvote, "resolve_choice_poll", new=AsyncMock()) as mock_res_choice:
                with patch("pokesketch.bot.datetime", wraps=__import__("datetime").datetime) as mock_dt:
                    mock_dt.now = MagicMock(return_value=MagicMock(date=lambda: target_date))
                    _run(bot._run_vote_step(1, "UTC"))
    mock_cat.assert_not_awaited()
    mock_res_cat.assert_not_awaited()
    mock_res_choice.assert_not_awaited()


def test_schedule_all_guilds_has_no_vote_jobs(fresh_db):
    bot = _make_bot()

    async def setup():
        async with db.session() as s:
            cfg = db.GuildConfig(guild_id=1, timezone="UTC", post_time="09:00", vote_day1_weekday=6)
            s.add(cfg)
            await s.commit()

    _run(setup())
    _run(bot._schedule_all_guilds())
    job_ids = {job.id for job in bot.scheduler.get_jobs()}
    assert not any(job_id.startswith(("vote-day1-", "vote-day2-", "vote-resolve-")) for job_id in job_ids)
    assert "daily-1" in job_ids


def test_daily_job_runs_sketch_then_wild_then_vote(fresh_db):
    bot = _make_bot()

    async def setup():
        async with db.session() as s:
            cfg = db.GuildConfig(guild_id=1, timezone="UTC", post_time="09:00", vote_day1_weekday=6)
            s.add(cfg)
            await s.commit()

    _run(setup())

    target_date = date(2026, 10, 11)  # Sunday = day 1
    order: list[str] = []

    async def _record_daily(client, api, guild_id, local_date):
        order.append("daily")

    async def _record_wild(client, api, guild_id, local_date):
        order.append("wild")

    recorded_vote = AsyncMock(side_effect=lambda _gid, _tz: order.append("vote"))
    with patch("pokesketch.bot.post_daily_for_guild", new=_record_daily):
        with patch("pokesketch.bot.post_wild_encounter_for_guild", new=_record_wild):
            with patch("pokesketch.bot.datetime", wraps=__import__("datetime").datetime) as mock_dt:
                mock_dt.now = MagicMock(return_value=MagicMock(date=lambda: target_date))
                with patch.object(bot, "_run_vote_step", new=recorded_vote):
                    _run(bot._run_daily_job(1, "UTC"))

    assert order == ["daily", "wild", "vote"]
    recorded_vote.assert_awaited_once_with(1, "UTC")
