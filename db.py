"""Database utilities — aiosqlite with WAL mode, async session factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import aiosqlite

DB_PATH = "orchestrator.db"


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """FastAPI dependency: provides an aiosqlite connection per request."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Initialize database tables on startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                subscription_type TEXT NOT NULL DEFAULT 'free',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                input_data TEXT DEFAULT '{}',
                output_data TEXT DEFAULT '{}',
                error TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()
