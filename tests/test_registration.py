"""Unit tests for /register: role grant, tutorial DM, cooldown, failures."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime

import discord
from discord import app_commands
from sqlalchemy import select

from pokesketch import db
from pokesketch.cogs.registration import (
    REGISTER_COOLDOWN_PER,
    Registration,
    _get_or_create_guild_user,
    _register_cooldown_bucket,
)
from pokesketch.embeds import tutorial_embed


def _run(coro):
    return asyncio.run(coro)


def _fresh_db():
    tmp = tempfile.mkdtemp()
    db.init_engine(os.path.join(tmp, "test.db"))
    return db.create_all()


class _FakeRole:
    def __init__(self, role_id: int):
        self.id = role_id


class _FakeMember:
    def __init__(self, user_id: int, guild_id: int, roles: list[_FakeRole] | None = None):
        self.id = user_id
        self.guild_id = guild_id
        self.roles = list(roles or [])
        self.dm_sent: list[dict] = []
        self.dm_forbidden = False
        self.add_roles_calls: list[list] = []
        self.add_roles_forbidden = False

    async def send(self, **kwargs):
        if self.dm_forbidden:
            raise discord.Forbidden(_FakeResponse(), "cannot dm")
        self.dm_sent.append(kwargs)

    async def add_roles(self, role, *, reason=None):
        self.add_roles_calls.append([role, reason])
        if self.add_roles_forbidden:
            raise discord.Forbidden(_FakeResponse(), "role hierarchy")
        if role not in self.roles:
            self.roles.append(role)


class _FakeGuild:
    def __init__(self, guild_id: int, role_id: int):
        self.id = guild_id
        self._role = _FakeRole(role_id)

    def get_role(self, role_id: int):
        return self._role if role_id == self._role.id else None


class _FakeResponse:
    def __init__(self):
        self.deferred = False
        self.sent: list[dict] = []

    @property
    def status(self) -> int:
        return 403

    @property
    def reason(self) -> str:
        return "Forbidden"

    async def defer(self, *, thinking: bool = False, ephemeral: bool = False):
        self.deferred = True

    async def send_message(self, content=None, *, ephemeral: bool = False, **kwargs):
        self.sent.append({"content": content, "ephemeral": ephemeral, "kwargs": kwargs})


class _FakeFollowup:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, content=None, *, ephemeral: bool = False, **kwargs):
        self.sent.append({"content": content, "ephemeral": ephemeral, "kwargs": kwargs})
        return None


class _FakeInteraction:
    def __init__(self, guild_id: int, user_id: int, *, role_id: int = 100, setup: bool = True):
        self.guild_id = guild_id
        self.user = _FakeMember(user_id, guild_id)
        self.guild = _FakeGuild(guild_id, role_id) if setup else None
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def _bound_callback(cog):
    """Return the underlying callback callable, bypassing the Command wrapper."""
    return cog.register_cmd.callback


async def _cog():
    await _fresh_db()
    return Registration(None)


def test_tutorial_embed_copy_matches_spec():
    embed = tutorial_embed()
    assert embed.title == "🎨 Welcome to PokeSketchDex!"
    assert embed.description == "You're registered! Here's how it all works."
    assert embed.color.value == 0x5865F2
    field_names = [f.name for f in embed.fields]
    assert "📕 Your PokéBox 🚧" in field_names
    assert "Coming soon" in field_names
    # Only one embed field name should carry the 🚧 marker.
    assert sum(f.name.count("🚧") for f in embed.fields) == 1
    # No blank field names (the trailing /help note is appended to Coming soon).
    assert field_names.count("") == 0
    assert embed.footer.text == "PokeSketchDex • Sketch 'em all"
    # All fields under Discord's value limit.
    for field in embed.fields:
        assert len(field.value) <= 1024


def test_register_success_grants_role_and_sends_dm_and_writes_column():
    async def scenario():
        cog = await _cog()
        callback = _bound_callback(cog)
        guild_id, user_id, role_id = 1, 2, 100
        interaction = _FakeInteraction(guild_id, user_id, role_id=role_id)
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, role_id=role_id, channel_id=10))
            await s.commit()

        await callback(cog, interaction)

        assert interaction.response.deferred
        assert len(interaction.user.dm_sent) == 1
        assert interaction.user.dm_sent[0]["embed"].title == "🎨 Welcome to PokeSketchDex!"
        assert len(interaction.user.add_roles_calls) == 1
        assert interaction.user.add_roles_calls[0][0].id == role_id
        assert any(r.id == role_id for r in interaction.user.roles)
        assert len(interaction.followup.sent) == 1
        assert "registered" in interaction.followup.sent[0]["content"].lower()

        async with db.session() as s:
            user = (await s.execute(
                select(db.User).where(db.User.guild_id == guild_id, db.User.user_id == user_id)
            )).scalar_one()
        assert user.registered_at is not None

        await db.dispose()

    _run(scenario())


def test_register_no_setup_replies_ephemeral():
    async def scenario():
        cog = await _cog()
        callback = _bound_callback(cog)
        interaction = _FakeInteraction(10, 20, setup=False)
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=10, role_id=None, channel_id=10))
            await s.commit()

        await callback(cog, interaction)

        assert interaction.response.deferred
        assert len(interaction.user.dm_sent) == 0
        assert len(interaction.user.add_roles_calls) == 0
        assert len(interaction.followup.sent) == 1
        assert "/setup" in interaction.followup.sent[0]["content"]

        await db.dispose()

    _run(scenario())


def test_register_missing_role_replies_setup():
    async def scenario():
        cog = await _cog()
        callback = _bound_callback(cog)
        guild_id, user_id, role_id = 11, 22, 100
        interaction = _FakeInteraction(guild_id, user_id, role_id=role_id)
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, role_id=role_id + 1, channel_id=10))
            await s.commit()

        await callback(cog, interaction)

        assert len(interaction.user.dm_sent) == 0
        assert len(interaction.user.add_roles_calls) == 0
        assert len(interaction.followup.sent) == 1
        assert "/setup" in interaction.followup.sent[0]["content"]

        await db.dispose()

    _run(scenario())


def test_register_dms_closed_does_not_grant_role():
    async def scenario():
        cog = await _cog()
        callback = _bound_callback(cog)
        guild_id, user_id, role_id = 2, 3, 101
        interaction = _FakeInteraction(guild_id, user_id, role_id=role_id)
        interaction.user.dm_forbidden = True
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, role_id=role_id, channel_id=10))
            await s.commit()

        await callback(cog, interaction)

        assert len(interaction.user.dm_sent) == 0
        assert len(interaction.user.add_roles_calls) == 0
        assert not any(r.id == role_id for r in interaction.user.roles)
        assert len(interaction.followup.sent) == 1
        content = interaction.followup.sent[0]["content"]
        assert "DM" in content or "DMs" in content
        assert "enable DMs" in content

        await db.dispose()

    _run(scenario())


def test_register_role_hierarchy_forbidden_generic_message():
    async def scenario():
        cog = await _cog()
        callback = _bound_callback(cog)
        guild_id, user_id, role_id = 3, 4, 102
        interaction = _FakeInteraction(guild_id, user_id, role_id=role_id)
        interaction.user.add_roles_forbidden = True
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, role_id=role_id, channel_id=10))
            await s.commit()

        await callback(cog, interaction)

        assert len(interaction.user.dm_sent) == 1
        assert len(interaction.user.add_roles_calls) == 1
        assert not any(r.id == role_id for r in interaction.user.roles)
        assert len(interaction.followup.sent) == 1
        content = interaction.followup.sent[0]["content"]
        assert "admin" in content.lower()
        assert "permissions" in content.lower()
        assert "Forbidden" not in content

        await db.dispose()

    _run(scenario())


def test_register_already_registered_resends_dm():
    async def scenario():
        cog = await _cog()
        callback = _bound_callback(cog)
        guild_id, user_id, role_id = 4, 5, 103
        role = _FakeRole(role_id)
        interaction = _FakeInteraction(guild_id, user_id, role_id=role_id)
        interaction.user.roles.append(role)
        async with db.session() as s:
            s.add(db.GuildConfig(guild_id=guild_id, role_id=role_id, channel_id=10))
            await s.commit()
        # Pre-seed the users row with a prior registration so the code recognises
        # the member as already registered (decision 4: resend path).
        async with db.session() as s:
            s.add(db.User(guild_id=guild_id, user_id=user_id, registered_at=datetime.now(UTC)))
            await s.commit()

        await callback(cog, interaction)

        assert len(interaction.user.dm_sent) == 1
        assert len(interaction.user.add_roles_calls) == 1
        assert len(interaction.followup.sent) == 1
        assert "already registered" in interaction.followup.sent[0]["content"].lower()

        await db.dispose()

    _run(scenario())


def test_cooldown_key_is_per_user_per_guild():
    i1 = _FakeInteraction(100, 50)
    i2 = _FakeInteraction(200, 50)
    assert _register_cooldown_bucket(i1) == (100, 50)
    assert _register_cooldown_bucket(i2) == (200, 50)
    assert _register_cooldown_bucket(i1) != _register_cooldown_bucket(i2)


def test_cooldown_handler_replies_ephemerally():
    async def scenario():
        cog = await _cog()
        error = app_commands.CommandOnCooldown(
            cooldown=app_commands.Cooldown(1, REGISTER_COOLDOWN_PER),
            retry_after=42.7,
        )
        interaction = _FakeInteraction(5, 6)
        await cog.register_cmd_error(interaction, error)

        assert len(interaction.response.sent) == 1
        reply = interaction.response.sent[0]
        assert reply["ephemeral"] is True
        assert "Slow down" in reply["content"]
        assert "42s" in reply["content"]
        assert "/register" in reply["content"]

        await db.dispose()

    _run(scenario())


def test_cooldown_handler_re_raises_non_cooldown_errors():
    async def scenario():
        cog = await _cog()
        interaction = _FakeInteraction(5, 6)
        try:
            await cog.register_cmd_error(interaction, RuntimeError("boom"))
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("non-cooldown error should be re-raised")

        await db.dispose()

    _run(scenario())


def test_get_or_create_user_creates_and_retrieves():
    async def scenario():
        await _fresh_db()
        async with db.session() as s:
            user = await _get_or_create_guild_user(s, 7, 8)
            await s.commit()
        async with db.session() as s:
            same = await _get_or_create_guild_user(s, 7, 8)
        assert user.id == same.id
        await db.dispose()

    _run(scenario())
