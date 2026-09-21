"""SQLAlchemy ORM models for PokeSketch.

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
