"""Tests for wait_for_poll_result() in weeklyvote.py."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import discord

from pokesketch import weeklyvote


def _run(coro):
    return asyncio.run(coro)


def _make_poll_result_message(message_id: int) -> MagicMock:
    msg = MagicMock()
    msg.type = discord.MessageType.poll_result
    msg.reference = MagicMock()
    msg.reference.message_id = message_id
    return msg


def _make_regular_message(message_id: int) -> MagicMock:
    msg = MagicMock()
    msg.type = discord.MessageType.default
    msg.reference = MagicMock()
    msg.reference.message_id = message_id
    return msg


def test_wait_for_poll_result_found():
    target_id = 12345
    found_msg = _make_poll_result_message(target_id)

    async def history():
        yield _make_regular_message(999)
        yield found_msg

    channel = MagicMock()
    channel.history = MagicMock(return_value=history())

    result = _run(weeklyvote.wait_for_poll_result(channel, target_id, timeout=0.05, interval=0.01))

    assert result is found_msg
    channel.history.assert_called_once_with(limit=20)


def test_wait_for_poll_result_not_found_times_out():
    target_id = 12345

    async def history():
        yield _make_regular_message(111)
        yield _make_poll_result_message(222)

    channel = MagicMock()
    channel.history = MagicMock(return_value=history())

    result = _run(weeklyvote.wait_for_poll_result(channel, target_id, timeout=0.05, interval=0.01))

    assert result is None


def test_wait_for_poll_result_ignores_wrong_reference():
    target_id = 12345

    async def history():
        yield _make_poll_result_message(target_id + 1)
        yield _make_poll_result_message(target_id - 1)

    channel = MagicMock()
    channel.history = MagicMock(return_value=history())

    result = _run(weeklyvote.wait_for_poll_result(channel, target_id, timeout=0.05, interval=0.01))

    assert result is None


def test_wait_for_poll_result_appears_after_initial_scan():
    target_id = 12345
    found_msg = _make_poll_result_message(target_id)
    calls = []

    async def history(*, limit: int):
        assert limit == 20
        if not calls:
            calls.append(1)
            yield _make_regular_message(111)
            return
        calls.append(2)
        yield found_msg

    channel = MagicMock()
    channel.history = MagicMock(side_effect=history)

    result = _run(weeklyvote.wait_for_poll_result(channel, target_id, timeout=0.1, interval=0.01))

    assert result is found_msg
    assert len(calls) == 2
