"""
database.py — SIDES Bot Database Layer
========================================
Async SQLAlchemy engine configuration and session management.

Usage:
    from database import init_db, get_session

    # At startup:
    await init_db()

    # In handlers / services:
    async with get_session() as session:
        user = await session.get(User, user_id)
        session.add(some_new_object)
        await session.commit()
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import DATABASE_URL, IS_PRODUCTION
from models import Base

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def _create_engine() -> AsyncEngine:
    """Create the async SQLAlchemy engine with appropriate settings."""
    connect_args: dict = {}

    if DATABASE_URL.startswith("sqlite"):
        # SQLite requires check_same_thread=False in async context
        connect_args["check_same_thread"] = False

    engine = create_async_engine(
        DATABASE_URL,
        echo=not IS_PRODUCTION,          # Log SQL in development
        pool_pre_ping=True,              # Detect stale connections
        connect_args=connect_args,
    )
    logger.info("Database engine created: %s", DATABASE_URL.split("///")[0])
    return engine


# Module-level engine (created once at import)
_engine: AsyncEngine = _create_engine()

# ---------------------------------------------------------------------------
# Session Factory
# ---------------------------------------------------------------------------

_AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,   # Don't expire attributes after commit
    autocommit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """
    Create all database tables if they don't exist.
    Call this once at application startup before processing any updates.
    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS semantics.
    """
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized — all tables created/verified.")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that yields a database session.

    Commits on clean exit, rolls back on exception.

    Example:
        async with get_session() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()
    """
    async with _AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_engine() -> AsyncEngine:
    """Return the shared async engine (for advanced use cases)."""
    return _engine


async def close_db() -> None:
    """Dispose of the engine and all connections. Call at shutdown."""
    await _engine.dispose()
    logger.info("Database connections closed.")
