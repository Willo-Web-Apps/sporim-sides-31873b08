"""
db.py — SideUp Bot Database Setup
====================================
Async SQLAlchemy engine configuration, session management, and
table initialisation. This is the canonical db layer used by all handlers
and services.

Usage:
    from db import init_db, get_db

    # At startup:
    await init_db()

    # In any handler / service:
    async with get_db() as session:
        user = await session.get(User, pk)
        session.add(new_record)
        # session auto-commits on clean exit
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
# Engine (module-level singleton)
# ---------------------------------------------------------------------------

def _make_engine() -> AsyncEngine:
    """
    Create the async SQLAlchemy engine with environment-appropriate settings.

    SQLite:
        - Uses aiosqlite driver
        - check_same_thread=False required for async usage
        - echo=True in development for query visibility

    PostgreSQL (production):
        - Uses asyncpg driver
        - pool_pre_ping detects stale connections after sleep/restart
    """
    connect_args: dict = {}

    if DATABASE_URL.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_async_engine(
        DATABASE_URL,
        echo=not IS_PRODUCTION,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    logger.info(
        "Async DB engine created: driver=%s",
        DATABASE_URL.split("+")[1].split(":")[0] if "+" in DATABASE_URL else "default",
    )
    return engine


# Module-level engine — created once at import time.
engine: AsyncEngine = _make_engine()

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,   # Attributes remain accessible after commit
    autocommit=False,
    autoflush=False,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """
    Create all ORM-defined tables if they do not already exist.

    Uses CREATE TABLE IF NOT EXISTS semantics — safe to call on every startup.
    Call this once before processing any Telegram updates.

    Raises:
        sqlalchemy.exc.OperationalError: If the database is unreachable.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialised — all tables verified.")


@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that yields a database session.

    Behaviour:
        - Commits automatically on clean exit
        - Rolls back on any exception
        - Always closes the session in the finally block

    Example:
        async with get_db() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()

    Yields:
        AsyncSession: An active SQLAlchemy async session.

    Raises:
        Re-raises any exception from the body after rolling back.
    """
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def close_db() -> None:
    """
    Dispose of all connections in the engine pool.

    Call this at application shutdown to release database resources cleanly.
    """
    await engine.dispose()
    logger.info("Database connections closed.")
