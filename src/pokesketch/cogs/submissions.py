"""Submission + upvote handling.

Submissions are made with /submit (an image attachment) inside a daily thread.
Running /submit in a thread targets that thread's day, so users can contribute
to older days by submitting in the older thread. Upvotes are 👍 reactions on
submission messages.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from .. import db, leveling, pokebox
from ..formatting import species_display_name

log = logging.getLogger(__name__)

UPVOTE_EMOJI = "👍"


def _submission_header(display_name: str, user_mention: str) -> str:
    return f"🖼️ {display_name} by {user_mention} — react {UPVOTE_EMOJI} to upvote!"


def _submission_confirmation(display_name: str, first_time: bool, exp_msg: str) -> str:
    if first_time:
        return f"✅ {display_name} submitted{exp_msg}!"
    return f"✅ Updated your {display_name} sketch!"


def _is_outside_grace_window(daily_local_date: date, today_local: date, grace_period_days: int) -> bool:
    return daily_local_date < today_local - timedelta(days=grace_period_days)


async def _get_or_create_user(s, guild_id: int, user_id: int) -> db.User:
    user = (
        await s.execute(
            select(db.User).where(db.User.guild_id == guild_id, db.User.user_id == user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        user = db.User(guild_id=guild_id, user_id=user_id)
        s.add(user)
        await s.flush()
    return user


async def _get_or_create_global_user(s, user_id: int) -> db.GlobalUser:
    user = (
        await s.execute(select(db.GlobalUser).where(db.GlobalUser.user_id == user_id))
    ).scalar_one_or_none()
    if user is None:
        user = db.GlobalUser(user_id=user_id)
        s.add(user)
        await s.flush()
    return user


async def _award_exp(s, guild_id: int, user_id: int, kind: str, amount: int) -> None:
    if amount == 0:
        return
    s.add(db.ExpEvent(guild_id=guild_id, user_id=user_id, type=kind, amount=amount))
    user = await _get_or_create_user(s, guild_id, user_id)
    user.exp += amount
    user.level = leveling.level_for_exp(user.exp)

    global_user = await _get_or_create_global_user(s, user_id)
    global_user.exp += amount
    global_user.level = leveling.level_for_exp(global_user.exp)


async def _find_wild_encounter_for_thread(
    s, thread_id: int, guild_id: int, today_local: date
) -> db.WildEncounter | None:
    """If `thread_id` is a guild's wild-encounter thread (WeeklyVote.thread_id),
    return today's WildEncounter row for it, or None if the thread doesn't
    match, or today's pick hasn't posted yet."""
    wv = (
        await s.execute(
            select(db.WeeklyVote).where(db.WeeklyVote.guild_id == guild_id, db.WeeklyVote.thread_id == thread_id)
        )
    ).scalar_one_or_none()
    if wv is None:
        return None
    return (
        await s.execute(
            select(db.WildEncounter).where(
                db.WildEncounter.weekly_vote_id == wv.id, db.WildEncounter.local_date == today_local
            )
        )
    ).scalar_one_or_none()


class Submissions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="submit",
        description="Submit your sketch for the day (run inside a daily or wild-encounter thread).",
    )
    @app_commands.describe(image="Your sketch image")
    async def submit(self, interaction: discord.Interaction, image: discord.Attachment) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await interaction.response.send_message(
                "Run `/submit` inside a daily sketch thread.", ephemeral=True
            )
            return
        if not (image.content_type or "").startswith("image/"):
            await interaction.response.send_message(
                "That attachment isn't an image.", ephemeral=True
            )
            return

        gid, uid = interaction.guild_id, interaction.user.id
        await interaction.response.defer(thinking=True)

        async with db.session() as s:
            daily = (
                await s.execute(
                    select(db.DailyPokemon).where(db.DailyPokemon.thread_id == channel.id)
                )
            ).scalar_one_or_none()

            if daily is not None:
                confirmation, _exp_msg = await self._submit_to_daily(
                    s, daily, channel, image, gid, uid, interaction.user.mention
                )
                await s.commit()
                await interaction.followup.send(confirmation, ephemeral=True)
                return

            cfg = await s.get(db.GuildConfig, gid)
            tz = cfg.timezone if cfg else "UTC"
            today_local = datetime.now(ZoneInfo(tz)).date()
            wild_encounter = await _find_wild_encounter_for_thread(s, channel.id, gid, today_local)
            if wild_encounter is None:
                await interaction.followup.send(
                    "This thread isn't a recognized daily or wild-encounter thread (or today's "
                    "wild encounter hasn't posted yet).",
                    ephemeral=True,
                )
                return

            confirmation, _exp_msg = await self._submit_to_wild_encounter(
                s, wild_encounter, channel, image, gid, uid, interaction.user.mention, today_local
            )
            await s.commit()

        await interaction.followup.send(confirmation, ephemeral=True)

    async def _submit_to_daily(
        self,
        s,
        daily: db.DailyPokemon,
        channel: discord.Thread,
        image: discord.Attachment,
        gid: int,
        uid: int,
        mention: str,
    ) -> tuple[str, str]:
        cfg = await s.get(db.GuildConfig, gid)
        grace_period_days = cfg.grace_period_days if cfg else 7
        tz = cfg.timezone if cfg else "UTC"
        today_local = datetime.now(ZoneInfo(tz)).date()
        if _is_outside_grace_window(daily.local_date, today_local, grace_period_days):
            return (
                f"This thread's Pokémon is outside the {grace_period_days}-day submission "
                "window and can no longer accept new sketches.",
                "",
            )

        display_name = species_display_name(daily.name)

        # One submission per user per day (updates image if re-submitting).
        existing = (
            await s.execute(
                select(db.Submission).where(db.Submission.daily_id == daily.id, db.Submission.user_id == uid)
            )
        ).scalar_one_or_none()

        posted = await channel.send(
            content=_submission_header(display_name, mention),
            file=await image.to_file(),
        )
        await posted.add_reaction(UPVOTE_EMOJI)

        first_time = existing is None
        if existing is None:
            sub = db.Submission(
                guild_id=gid, user_id=uid, daily_id=daily.id,
                message_id=posted.id, image_url=posted.attachments[0].url,
            )
            s.add(sub)
            await s.flush()  # assign sub.id for the PokeBox pointer below
        else:
            existing.message_id = posted.id
            existing.image_url = posted.attachments[0].url
            sub = existing

        # PokeBox: every submitted sketch scans that dex number into the player's
        # Pokédex (free, automatic, insert-if-not-exists).
        await pokebox.record_pokebox_scan(s, uid, daily.dex_no, sub.id)

        exp_msg = ""
        if first_time:
            total = await self._award_submission_rewards(s, gid, uid, daily.local_date, leveling.EXP_SUBMIT, "submit")
            exp_msg = f" (+{total} EXP)"

        return _submission_confirmation(display_name, first_time, exp_msg), exp_msg

    async def _submit_to_wild_encounter(
        self,
        s,
        wild_encounter: db.WildEncounter,
        channel: discord.Thread,
        image: discord.Attachment,
        gid: int,
        uid: int,
        mention: str,
        today_local: date,
    ) -> tuple[str, str]:
        display_name = species_display_name(wild_encounter.name)

        # One submission per user per day per wild encounter (updates image if re-submitting).
        existing = (
            await s.execute(
                select(db.WildEncounterSubmission).where(
                    db.WildEncounterSubmission.wild_encounter_id == wild_encounter.id,
                    db.WildEncounterSubmission.user_id == uid,
                )
            )
        ).scalar_one_or_none()

        posted = await channel.send(
            content=_submission_header(display_name, mention),
            file=await image.to_file(),
        )
        await posted.add_reaction(UPVOTE_EMOJI)

        first_time = existing is None
        if existing is None:
            sub = db.WildEncounterSubmission(
                guild_id=gid, user_id=uid, wild_encounter_id=wild_encounter.id,
                message_id=posted.id, image_url=posted.attachments[0].url,
            )
            s.add(sub)
            await s.flush()  # assign sub.id for the PokeBox pointer below
        else:
            existing.message_id = posted.id
            existing.image_url = posted.attachments[0].url
            sub = existing

        await pokebox.record_pokebox_scan(s, uid, wild_encounter.dex_no, None)

        exp_msg = ""
        if first_time:
            total = await self._award_submission_rewards(
                s, gid, uid, today_local, leveling.EXP_WILD_ENCOUNTER, "wild_submit"
            )
            exp_msg = f" (+{total} EXP)"

        return _submission_confirmation(display_name, first_time, exp_msg), exp_msg

    @staticmethod
    async def _award_submission_rewards(s, gid: int, uid: int, submit_date: date, base_exp: int, kind: str) -> int:
        """Shared EXP/streak/guild-stats path for a first-time submission of the
        day, to either the main daily or wild-encounter thread. Reuses the
        same streak logic either way, so at most one streak increment lands
        per day — globally and per-guild — regardless of which thread(s) a
        user submits to. Returns the total EXP granted (base + streak bonus).
        """
        await _award_exp(s, gid, uid, kind, base_exp)
        user = await _get_or_create_user(s, gid, uid)
        bonus = Submissions._update_streak(user, submit_date)
        if bonus:
            await _award_exp(s, gid, uid, "streak", bonus)
        global_user = await _get_or_create_global_user(s, uid)
        Submissions._update_global_streak(global_user, submit_date)
        await Submissions._update_guild_stats(s, gid, submit_date)
        return base_exp + bonus

    @staticmethod
    def _update_streak(user: db.User, submit_date: date) -> int:
        """Advance personal streak if this is a new day's submission. Returns streak bonus."""
        last = user.last_submit_date
        if last == submit_date:
            return 0  # already counted today
        if last == submit_date - timedelta(days=1):
            user.personal_streak += 1
        else:
            user.personal_streak = 1
        user.last_submit_date = submit_date
        user.longest_streak = max(user.longest_streak, user.personal_streak)
        return leveling.streak_bonus(user.personal_streak)

    @staticmethod
    def _update_global_streak(user: db.GlobalUser, submit_date: date) -> int:
        """Advance the cross-server streak ("any guild" counts). No bonus EXP — stat only."""
        last = user.last_submit_date
        if last == submit_date:
            return 0  # already counted today (in some other guild)
        if last == submit_date - timedelta(days=1):
            user.global_streak += 1
        else:
            user.global_streak = 1
        user.last_submit_date = submit_date
        user.longest_global_streak = max(user.longest_global_streak, user.global_streak)
        return user.global_streak

    @staticmethod
    async def _update_guild_stats(s, guild_id: int, submit_date: date) -> None:
        stats = await s.get(db.GuildStats, guild_id)
        if stats is None:
            # Explicit zeros: column `default=` values aren't applied to the Python
            # attribute until flush, and total_submissions += 1 below needs a real int.
            stats = db.GuildStats(guild_id=guild_id, global_streak=0, longest_global_streak=0, total_submissions=0)
            s.add(stats)
        stats.total_submissions += 1
        last = stats.last_active_date
        if last != submit_date:
            if last == submit_date - timedelta(days=1):
                stats.global_streak += 1
            elif last is None or submit_date > last:
                stats.global_streak = 1
            stats.last_active_date = submit_date
            stats.longest_global_streak = max(stats.longest_global_streak, stats.global_streak)

    # --- Upvote tracking via reactions ---

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload, added=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload, added=False)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent, added: bool) -> None:
        if str(payload.emoji) != UPVOTE_EMOJI or payload.guild_id is None:
            return
        if self.bot.user and payload.user_id == self.bot.user.id:
            return
        async with db.session() as s:
            sub = (
                await s.execute(
                    select(db.Submission).where(db.Submission.message_id == payload.message_id)
                )
            ).scalar_one_or_none()
            if sub is None:
                return
            if payload.user_id == sub.user_id:
                return  # no self-upvotes
            existing = (
                await s.execute(
                    select(db.Upvote).where(
                        db.Upvote.submission_id == sub.id, db.Upvote.voter_id == payload.user_id
                    )
                )
            ).scalar_one_or_none()
            if added and existing is None:
                s.add(db.Upvote(submission_id=sub.id, voter_id=payload.user_id))
                await self._maybe_award_upvote_exp(s, sub)
            elif not added and existing is not None:
                await s.delete(existing)
            await s.commit()

    @staticmethod
    async def _maybe_award_upvote_exp(s, sub: db.Submission) -> None:
        """Award +1 EXP per upvote to the artist, capped per day."""
        today_total = (
            await s.execute(
                select(func.coalesce(func.sum(db.ExpEvent.amount), 0)).where(
                    db.ExpEvent.guild_id == sub.guild_id,
                    db.ExpEvent.user_id == sub.user_id,
                    db.ExpEvent.type == "upvote",
                    func.date(db.ExpEvent.created_at) == func.date(func.now()),
                )
            )
        ).scalar_one()
        if today_total < leveling.EXP_UPVOTE_DAILY_CAP:
            await _award_exp(s, sub.guild_id, sub.user_id, "upvote", leveling.EXP_PER_UPVOTE)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Submissions(bot))
