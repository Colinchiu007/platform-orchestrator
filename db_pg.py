"""Phase B: PostgreSQL database layer — SQLAlchemy async engine and session.

Provides get_db_pg as a FastAPI dependency replacement for get_db (SQLite).
Only serves auth-related tables (users, refresh_tokens, subscriptions).
Other routers continue using db.py:get_db (aiosqlite) until full migration.

Schema: auth (created automatically on init)
Connection: shared PG instance with trendscope
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from models.auth_models import Base as AuthBase

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db_pg() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: provides a SQLAlchemy AsyncSession per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_pg_db() -> None:
    """Initialize auth tables on startup. Falls back to SQLite for dev."""
    try:
        async with engine.begin() as conn:
            is_sqlite = settings.database_url.startswith("sqlite")
            if not is_sqlite:
                # PostgreSQL-specific: create auth schema and set search path
                await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {settings.db_auth_schema}"))
                await conn.execute(text(f"SET search_path TO {settings.db_auth_schema}, public"))
            else:
                # SQLite has no schema support — strip "auth." prefix from table names
                for table in AuthBase.metadata.tables.values():
                    table.schema = None
            # Create tables from ORM models (works for both PG and SQLite)
            await conn.run_sync(AuthBase.metadata.create_all)
    except Exception as exc:
        import logging
        logging.warning(f"Database unavailable, skipping init_pg_db: {exc}")
