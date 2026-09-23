"""SQLAlchemy ORM models for PokeSketchDex.

Multi-guild by design: every row that is guild-scoped carries ``guild_id`` so a
single bot instance can serve many servers with isolated config and data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class GuildConfig(Base):
    """Per-guild settings, configured via /setup slash commands."""

    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Local post time as "HH:MM" 24h, interpreted in `timezone`.
    post_time: Mapped[str] = mapped_column(String(5), default="09:00")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    selection_mode: Mapped[str] = mapped_column(String(32), default="no_repeat")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    # Inclusive national-dex range the daily pick draws from.
    dex_min: Mapped[int] = mapped_column(Integer, default=1)
    dex_max: Mapped[int] = mapped_column(Integer, default=1025)
    # Days back a thread stays open for /submit backfill (guild-local dates).
    grace_period_days: Mapped[int] = mapped_column(Integer, default=7)
    # Hours a submission stays catchable via /catch. Can never exceed
    # grace_period_days * 24 — enforced by /set-catch-window and re-clamped
    # by /set-grace-period if it's later lowered below this value.
    catch_window_hours: Mapped[int] = mapped_column(Integer, default=24)
    # Weekday the weekly category vote posts (0=Monday..6=Sunday, Python's
    # date.weekday() convention). Day 2 (specific-choice vote) is always the
    # next day and isn't independently configurable. Default Sunday.
    vote_day1_weekday: Mapped[int] = mapped_column(Integer, default=6)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    dailies: Mapped[list[DailyPokemon]] = relationship(
        back_populates="guild", cascade="all, delete-orphan"
    )


class DailyPokemon(Base):
    """The Pokemon chosen for a given guild on a given local date."""

    __tablename__ = "daily_pokemon"
    __table_args__ = (
        UniqueConstraint("guild_id", "local_date", name="uq_daily_guild_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guild_config.guild_id", ondelete="CASCADE"), index=True
    )
    local_date: Mapped[date] = mapped_column(Date, index=True)
    dex_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    is_shiny: Mapped[bool] = mapped_column(Boolean, default=False)
    # Newline-joined reference image URLs (1-2).
    ref_image_urls: Mapped[str] = mapped_column(String(1024), default="")
    # The bot's announcement message + the created submissions thread.
    announce_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    guild: Mapped[GuildConfig] = relationship(back_populates="dailies")
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="daily", cascade="all, delete-orphan"
    )

    @property
    def image_list(self) -> list[str]:
        return [u for u in self.ref_image_urls.splitlines() if u.strip()]


class User(Base):
    """Per-guild user progression (a Discord user is tracked per guild)."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("guild_id", "user_id", name="uq_user_guild"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    exp: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    personal_streak: Mapped[int] = mapped_column(Integer, default=0)
    longest_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_submit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GlobalUser(Base):
    """Cross-server user progression (summed across every guild)."""

    __tablename__ = "global_users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    exp: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    global_streak: Mapped[int] = mapped_column(Integer, default=0)
    longest_global_streak: Mapped[int] = mapped_column(Integer, default=0)
    last_submit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Submission(Base):
    """A sketch submitted by a user for a given daily."""

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    daily_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("daily_pokemon.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    image_url: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    daily: Mapped[DailyPokemon] = relationship(back_populates="submissions")
    upvotes: Mapped[list[Upvote]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )

    @property
    def dex_no(self) -> int:
        return self.daily.dex_no

    @property
    def species_name(self) -> str:
        return self.daily.name

    @property
    def is_shiny(self) -> bool:
        return self.daily.is_shiny


class Upvote(Base):
    """A single upvote of a submission by a voter (unique per voter)."""

    __tablename__ = "upvotes"
    __table_args__ = (
        UniqueConstraint("submission_id", "voter_id", name="uq_upvote_voter"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    voter_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    submission: Mapped[Submission] = relationship(back_populates="upvotes")


class ExpEvent(Base):
    """Append-only audit log of EXP grants, so the formula can be re-tuned."""

    __tablename__ = "exp_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GuildStats(Base):
    """Server-wide running aggregates (global streak, totals)."""

    __tablename__ = "guild_stats"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    global_streak: Mapped[int] = mapped_column(Integer, default=0)
    longest_global_streak: Mapped[int] = mapped_column(Integer, default=0)
    total_submissions: Mapped[int] = mapped_column(Integer, default=0)
    last_active_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class UsedPokemon(Base):
    """Tracks which dex numbers a guild has already used (no-repeat mode)."""

    __tablename__ = "used_pokemon"
    __table_args__ = (
        UniqueConstraint("guild_id", "dex_no", name="uq_used_guild_dex"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    dex_no: Mapped[int] = mapped_column(Integer)
    used_on: Mapped[date] = mapped_column(Date, default=lambda: datetime.now(UTC).date())


class PokeBox(Base):
    """Free, unlimited, global dex-completion tracker: has this user ever scanned this dex_no.

    Populated automatically inside /submit (insert-if-not-exists), no pokeball
    cost, no image copy — just points at the submission that scanned it.
    """

    __tablename__ = "pokebox"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dex_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    submission_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("submissions.id", ondelete="SET NULL"), nullable=True
    )


class PokeballWallet(Base):
    """Global pokeball balance, one per Discord user, shared across every guild."""

    __tablename__ = "pokeball_wallets"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # New wallets start with a starter grant (see pokebox.STARTER_POKEBALLS) rather
    # than 0, so a brand-new player can /catch immediately without waiting a week.
    balance: Mapped[int] = mapped_column(Integer, default=0)
    # ISO week the last weekly grant landed, e.g. "2026-W39" — idempotency guard
    # so a mid-week restart doesn't double-grant.
    last_granted_week: Mapped[str | None] = mapped_column(String(10), nullable=True)


class CaughtMon(Base):
    """A caught sketch image, global, capped at 20 total per user / 6 active.

    Active party (is_active=True) uses slot 1-6; storage box (is_active=False)
    uses a separate 1-20 box index. Both caps are enforced at write time, not
    by a DB constraint (SQLite has no worthwhile partial-unique support here).
    No uniqueness on (user_id, dex_no) — duplicate species are allowed.
    """

    __tablename__ = "caught_mons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    slot: Mapped[int] = mapped_column(Integer)
    dex_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    is_shiny: Mapped[bool] = mapped_column(Boolean, default=False)
    # Optional cosmetic overlay, separate from the authoritative species `name`.
    nickname: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Local file path on disk (data/party_cache/{user_id}/{id}.png), NOT a Discord URL.
    cached_image_path: Mapped[str] = mapped_column(String(1024))
    caught_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    source_submission_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("submissions.id", ondelete="SET NULL"), nullable=True
    )
    # Wild-encounter counterpart of source_submission_id — a caught mon points
    # at exactly one of the two (never both), depending which thread type it
    # was caught from.
    source_wild_encounter_submission_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("wild_encounter_submissions.id", ondelete="SET NULL"), nullable=True
    )
    # Reserved for the deferred leveling stretch phase — unused, left at defaults.
    mon_exp: Mapped[int] = mapped_column(Integer, default=0)
    mon_level: Mapped[int] = mapped_column(Integer, default=1)


class WeeklyVote(Base):
    """Per-guild, one row per ISO week: the two-stage category->choice vote
    that defines the week's wild-encounter Pokemon pool.

    Phase 1 (category): category_poll_message_id posted -> category set once
    the poll closes. Phase 2 (choice): choice_poll_message_id posted -> once
    that closes, resolved_dex_pool is cached and the pool is ready to serve
    daily wild-encounter posts (each with its own fresh thread — see
    WildEncounter.thread_id). Any of the poll/choice fields may be
    null while a cycle is still in progress.
    """

    __tablename__ = "weekly_votes"
    __table_args__ = (UniqueConstraint("guild_id", "iso_week", name="uq_weeklyvote_guild_week"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    iso_week: Mapped[str] = mapped_column(String(10))  # e.g. "2026-W39"
    category_poll_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # "location" | "type" | "event" | "habitat" | "generation"
    category: Mapped[str | None] = mapped_column(String(16), nullable=True)
    choice_poll_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Category-specific: location-area display name, type name, event name,
    # habitat display name, or region label.
    choice_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Newline-joined dex numbers, cached once day-2 resolves.
    resolved_dex_pool: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    wild_encounters: Mapped[list[WildEncounter]] = relationship(
        back_populates="weekly_vote", cascade="all, delete-orphan"
    )

    @property
    def dex_pool_numbers(self) -> list[int]:
        if not self.resolved_dex_pool:
            return []
        return [int(x) for x in self.resolved_dex_pool.splitlines() if x.strip()]


class EventDefinition(Base):
    """Admin-authored, guild-scoped explicit species list for the Event category.

    No filter/rule-based definitions in v1 — an admin manually lists every dex
    number in the event. `/admin event disable` retires one without deleting
    it (reusable next time, e.g. an annual Halloween event).
    """

    __tablename__ = "event_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(128))
    flavor_text: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dex_list: Mapped[str] = mapped_column(String)  # newline-joined explicit dex numbers
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def dex_numbers(self) -> list[int]:
        return [int(x) for x in self.dex_list.splitlines() if x.strip()]


class LocationArea(Base):
    """Cached PokeAPI `location-area` lookup — shared cache, not guild-scoped.

    Primary key is the PokeAPI location-area id (from the curated list in
    weeklyvote.py), not autoincrement, so a re-fetch is a straight upsert.
    """

    __tablename__ = "location_areas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128))  # PokeAPI slug, e.g. "kanto-route-1-area"
    display_name: Mapped[str] = mapped_column(String(128))  # e.g. "Kanto Route 1"
    dex_pool: Mapped[str] = mapped_column(String)  # newline-joined dex numbers
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def dex_numbers(self) -> list[int]:
        return [int(x) for x in self.dex_pool.splitlines() if x.strip()]


class TypePool(Base):
    """Cached PokeAPI `/type/{name}` lookup — shared cache, not guild-scoped."""

    __tablename__ = "type_pools"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)  # type name, e.g. "fire"
    name: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128))
    dex_pool: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def dex_numbers(self) -> list[int]:
        return [int(x) for x in self.dex_pool.splitlines() if x.strip()]


class HabitatPool(Base):
    """Cached PokeAPI `/pokemon-habitat/{name}` lookup — shared cache, not guild-scoped."""

    __tablename__ = "habitat_pools"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)  # habitat name, e.g. "cave"
    name: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(128))
    dex_pool: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def dex_numbers(self) -> list[int]:
        return [int(x) for x in self.dex_pool.splitlines() if x.strip()]


class WildEncounter(Base):
    """The Pokemon posted into a guild's wild-encounter thread on a given local
    date — the wild-encounter counterpart of `DailyPokemon`. One row (and one
    fresh thread) per guild per day, created immediately after that day's main
    daily post, from that week's `WeeklyVote.resolved_dex_pool`. Not part of
    the design doc's explicit table list, but required to give `/submit`
    something to point at (mirrors how `DailyPokemon` anchors the main daily
    thread's submissions).
    """

    __tablename__ = "wild_encounters"
    __table_args__ = (UniqueConstraint("guild_id", "local_date", name="uq_wildencounter_guild_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    weekly_vote_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("weekly_votes.id", ondelete="CASCADE"), index=True
    )
    local_date: Mapped[date] = mapped_column(Date, index=True)
    dex_no: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Each day gets its own fresh companion thread (one Pokemon per thread),
    # created right after the day's announcement message.
    thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    weekly_vote: Mapped[WeeklyVote] = relationship(back_populates="wild_encounters")
    submissions: Mapped[list[WildEncounterSubmission]] = relationship(
        back_populates="wild_encounter", cascade="all, delete-orphan"
    )


class WildEncounterSubmission(Base):
    """A sketch submitted to a guild's wild-encounter thread — the
    wild-encounter counterpart of `Submission`.

    Kept as its own table rather than widening `Submission` (which would need
    a manual ALTER TABLE + NOT NULL relaxation on `daily_id` for every existing
    deployment) — a brand-new table only needs `create_all()`, no migration.
    """

    __tablename__ = "wild_encounter_submissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    wild_encounter_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wild_encounters.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    image_url: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    wild_encounter: Mapped[WildEncounter] = relationship(back_populates="submissions")

    @property
    def dex_no(self) -> int:
        return self.wild_encounter.dex_no

    @property
    def species_name(self) -> str:
        return self.wild_encounter.name

    @property
    def is_shiny(self) -> bool:
        # No shiny mechanic for wild encounters in v1.
        return False


class GymEvent(Base):
    """Admin-run, time-boxed community goal: a named leader with an HP pool
    that player contributions chip away at during the event window.

    Only one `active` GymEvent per guild at a time — enforced at the
    application level (see cogs/gym.py's /gym start), not a DB constraint.
    """

    __tablename__ = "gym_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(128))
    leader_name: Mapped[str] = mapped_column(String(128))
    hp_total: Mapped[int] = mapped_column(Integer)
    hp_remaining: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # "active" | "defeated" | "expired" | "cancelled"
    status: Mapped[str] = mapped_column(String(16), default="active")
    badge_name: Mapped[str] = mapped_column(String(128))
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GymContribution(Base):
    """Append-only audit log of gym-damage contributions, same pattern as `ExpEvent`."""

    __tablename__ = "gym_contributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gym_events.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # "submit" | "upvote_given" | "upvote_received"
    kind: Mapped[str] = mapped_column(String(32))
    damage: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GymBadge(Base):
    """Event trophy: one per player per defeated gym.

    Separate table from any future cosmetic level-based rank badges — see
    the design doc's decision on why these must never share a table.
    """

    __tablename__ = "gym_badges"
    __table_args__ = (UniqueConstraint("gym_event_id", "user_id", name="uq_gymbadge_event_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("gym_events.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    awarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
