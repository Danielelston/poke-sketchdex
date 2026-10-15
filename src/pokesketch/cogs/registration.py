"""/register command: self-service role grant + tutorial DM."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from .. import db
from ..embeds import tutorial_embed

log = logging.getLogger(__name__)

# 1 invocation per 5 minutes, per (guild_id, user_id). Discord.py's cooldown
# decorator accepts a callable `key` returning a hashable bucket.
REGISTER_COOLDOWN_RATE = 1
REGISTER_COOLDOWN_PER = 300


def _register_cooldown_bucket(interaction: discord.Interaction) -> tuple[int, int]:
    """Cooldown key: per-user-per-guild so registration in one server does not
    block another."""
    return (interaction.guild_id, interaction.user.id)


async def _get_or_create_guild_user(s, guild_id: int | None, user_id: int) -> db.User:
    """Get-or-create the per-guild users row; tolerates a losing race under
    SQLite's single-writer constraint."""
    user = (
        await s.execute(select(db.User).where(db.User.guild_id == guild_id, db.User.user_id == user_id))
    ).scalar_one_or_none()
    if user is None:
        user = db.User(guild_id=guild_id, user_id=user_id)
        s.add(user)
        await s.flush()
    return user


class Registration(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="register", description="Join the daily sketch challenge and receive the tutorial DM."
    )
    @app_commands.checks.cooldown(
        REGISTER_COOLDOWN_RATE,
        REGISTER_COOLDOWN_PER,
        key=_register_cooldown_bucket,
    )
    async def register_cmd(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        guild = interaction.guild
        member = interaction.user

        await interaction.response.defer(thinking=True, ephemeral=True)

        async with db.session() as s:
            cfg = await s.get(db.GuildConfig, guild_id)
            if cfg is None or cfg.role_id is None:
                log.warning("register failed: no setup for guild_id=%s user_id=%s", guild_id, user_id)
                await interaction.followup.send(
                    "This server hasn't been set up yet — ask an admin to run `/setup`.",
                    ephemeral=True,
                )
                return

            role = guild.get_role(cfg.role_id) if guild else None
            if role is None:
                log.warning(
                    "register failed: configured role missing for guild_id=%s user_id=%s",
                    guild_id,
                    user_id,
                )
                await interaction.followup.send(
                    "This server hasn't been set up yet — ask an admin to run `/setup`.",
                    ephemeral=True,
                )
                return

            # "Already registered" means the user row exists with a non-NULL
            # registered_at. Role presence alone can come from a manual admin
            # assignment; decision 4 resends the tutorial whenever registration
            # has previously succeeded.
            user = await _get_or_create_guild_user(s, guild_id, user_id)
            already_registered = user.registered_at is not None

            # DM gates the role grant: no role if the tutorial can't be delivered.
            dm_succeeded = False
            try:
                await member.send(embed=tutorial_embed())
                dm_succeeded = True
            except discord.Forbidden:
                log.warning(
                    "register failed: DMs closed for guild_id=%s user_id=%s", guild_id, user_id
                )
                await interaction.followup.send(
                    (
                        "I couldn't send you the tutorial DM — please enable DMs from server members, "
                        "then try `/register` again."
                    ),
                    ephemeral=True,
                )

            if not dm_succeeded:
                return

            # Role grant is second: only reached if DM succeeded.
            try:
                await member.add_roles(role, reason="Registered via /register")
            except discord.Forbidden:
                log.warning(
                    "register failed: role hierarchy for guild_id=%s user_id=%s", guild_id, user_id
                )
                await interaction.followup.send(
                    "Couldn't assign your role — ask a server admin to check the bot's permissions.",
                    ephemeral=True,
                )
                return

            if not already_registered:
                user.registered_at = datetime.now(UTC)
            await s.commit()

        if already_registered:
            log.info("register resend: guild_id=%s user_id=%s", guild_id, user_id)
            await interaction.followup.send(
                "You're already registered — I've sent the tutorial DM again.",
                ephemeral=True,
            )
            return

        log.info("register success: guild_id=%s user_id=%s", guild_id, user_id)
        await interaction.followup.send(
            "You're registered! Check your DMs for the tutorial.",
            ephemeral=True,
        )

    @register_cmd.error
    async def register_cmd_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        """Minimal, /register-only cooldown handler."""
        if isinstance(error, app_commands.CommandOnCooldown):
            retry = int(error.retry_after)
            await interaction.response.send_message(
                f"Slow down — try `/register` again in {retry}s.",
                ephemeral=True,
            )
            return
        # Re-raise anything else so discord.py prints the default trace; this
        # handler is intentionally scoped to cooldowns only.
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Registration(bot))
