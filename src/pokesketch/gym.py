"""Gym Events business logic: HP tracking, contribution damage, badges.

Base (contribution-only) version — see the Gym / Server Events design doc.
The mon-level-boosted variant is a later phase, not built here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from sqlalchemy import func, select

from . import db

log = logging.getLogger(__name__)

# --- Damage constants (see design doc's damage-weighting model) ---
GYM_DAMAGE_SUBMIT = 10
GYM_DAMAGE_UPVOTE_GIVEN = 2
GYM_DAMAGE_UPVOTE_RECEIVED = 1
GYM_DAMAGE_UPVOTE_RECEIVED_DAILY_CAP = 10  # per user, per gym event, per day

STATUS_ACTIVE = "active"
STATUS_DEFEATED = "defeated"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

KIND_SUBMIT = "submit"
KIND_UPVOTE_GIVEN = "upvote_given"
KIND_UPVOTE_RECEIVED = "upvote_received"


def naive_utcnow() -> datetime:
    """UTC now with tzinfo stripped — SQLite round-trips DateTime(timezone=True)
    columns as naive text, so `GymEvent.ends_at` comes back naive even though it
    was written as UTC-aware (same convention as pokebox._naive_utcnow)."""
    return datetime.now(UTC).replace(tzinfo=None)


async def get_active_gym_event(s, guild_id: int) -> db.GymEvent | None:
    return (
        await s.execute(
            select(db.GymEvent).where(
                db.GymEvent.guild_id == guild_id, db.GymEvent.status == STATUS_ACTIVE
            )
        )
    ).scalar_one_or_none()


async def _upvote_received_damage_today(s, gym_event_id: int, user_id: int) -> int:
    return (
        await s.execute(
            select(func.coalesce(func.sum(db.GymContribution.damage), 0)).where(
                db.GymContribution.gym_event_id == gym_event_id,
                db.GymContribution.user_id == user_id,
                db.GymContribution.kind == KIND_UPVOTE_RECEIVED,
                func.date(db.GymContribution.created_at) == func.date(func.now()),
            )
        )
    ).scalar_one()


async def record_contribution(
    s, guild_id: int, user_id: int, kind: str
) -> tuple[db.GymEvent, bool] | None:
    """Log a `GymContribution` and decrement HP for the guild's active gym
    event, if any — same transaction as the caller's own EXP award (caller
    is responsible for committing).

    Returns `(event, just_defeated)` if the guild has an active gym event,
    or `None` if it doesn't. `just_defeated` is True only on the exact call
    that dropped HP to 0, so the caller can announce exactly once.
    """
    event = await get_active_gym_event(s, guild_id)
    if event is None:
        return None

    if kind == KIND_SUBMIT:
        damage = GYM_DAMAGE_SUBMIT
    elif kind == KIND_UPVOTE_GIVEN:
        damage = GYM_DAMAGE_UPVOTE_GIVEN
    elif kind == KIND_UPVOTE_RECEIVED:
        today_damage = await _upvote_received_damage_today(s, event.id, user_id)
        if today_damage >= GYM_DAMAGE_UPVOTE_RECEIVED_DAILY_CAP:
            return event, False
        damage = GYM_DAMAGE_UPVOTE_RECEIVED
    else:
        raise ValueError(f"unknown gym contribution kind: {kind!r}")

    s.add(db.GymContribution(gym_event_id=event.id, user_id=user_id, kind=kind, damage=damage))
    event.hp_remaining -= damage

    just_defeated = False
    if event.hp_remaining <= 0:
        just_defeated = True
        await mark_defeated(s, event)

    return event, just_defeated


async def mark_defeated(s, event: db.GymEvent) -> None:
    """Transition `event` to defeated and award a `GymBadge` to every distinct
    contributor, skipping anyone who already has one (idempotent)."""
    event.hp_remaining = max(event.hp_remaining, 0)
    event.status = STATUS_DEFEATED
    await _award_badges(s, event)


async def _award_badges(s, event: db.GymEvent) -> None:
    contributor_ids = (
        await s.execute(
            select(db.GymContribution.user_id)
            .where(db.GymContribution.gym_event_id == event.id)
            .distinct()
        )
    ).scalars().all()
    already_badged = set(
        (
            await s.execute(
                select(db.GymBadge.user_id).where(db.GymBadge.gym_event_id == event.id)
            )
        ).scalars().all()
    )
    for user_id in contributor_ids:
        if user_id not in already_badged:
            s.add(db.GymBadge(gym_event_id=event.id, user_id=user_id))


async def close_expired_gym_events(s, now: datetime | None = None) -> list[db.GymEvent]:
    """Close every active GymEvent whose `ends_at` has passed. No badge is
    awarded — per the locked design, a timeout is a plain loss. `now` is
    naive-UTC, injectable for tests."""
    now = now if now is not None else naive_utcnow()
    events = (
        await s.execute(
            select(db.GymEvent).where(
                db.GymEvent.status == STATUS_ACTIVE, db.GymEvent.ends_at <= now
            )
        )
    ).scalars().all()
    for event in events:
        event.status = STATUS_EXPIRED
    return list(events)


async def announce_defeat(bot: discord.Client, event: db.GymEvent) -> None:
    """Post a "gym defeated!" announcement to `event.channel_id`."""
    if not event.channel_id:
        log.warning("Gym event %s (%s) defeated but has no channel_id to announce in.", event.id, event.name)
        return
    channel = bot.get_channel(event.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(event.channel_id)
        except discord.DiscordException:
            log.warning("Gym event %s: announce channel %s not found.", event.id, event.channel_id)
            return
    embed = discord.Embed(
        title=f"🏆 {event.leader_name} has been defeated!",
        description=(
            f"**{event.name}** is cleared! Every contributor earns the **{event.badge_name}** badge — "
            "check it out with `/gym badges`."
        ),
        color=0xF1C40F,
    )
    try:
        await channel.send(embed=embed)
    except discord.DiscordException:
        log.warning("Gym event %s: failed to post defeat announcement.", event.id)
