"""Async engine + session factory and schema init."""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(db_path: str) -> AsyncEngine:
    """Create the global async engine for a SQLite file path."""
    global _engine, _sessionmaker
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    _engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def create_all() -> None:
    """Create tables if they do not exist."""
    if _engine is None:
        raise RuntimeError("Engine not initialized; call init_engine() first.")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def session() -> AsyncSession:
    """Open a new async session."""
    if _sessionmaker is None:
        raise RuntimeError("Engine not initialized; call init_engine() first.")
    return _sessionmaker()


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()
