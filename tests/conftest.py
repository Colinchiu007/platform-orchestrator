"""Shared pytest fixtures for platform-orchestrator tests.

Handles one-time DB initialization, rate-limit bypass, and PostgreSQL fallback
to prevent event-loop conflicts and SQLite lock contention across test modules.

CRITICAL: The rate-limit monkeypatch MUST run at module level (before
any test module imports 'main'), NOT in a fixture, because slowapi
evaluates limit callables at decoration time during import.
"""

from __future__ import annotations

import asyncio
import os

# ── Env vars BEFORE any imports that trigger config.py ────────────────
os.environ.setdefault("PO_SECRET_KEY", "test-secret-key-change-me-in-production")
# Use two separate SQLite files: test.db for main tables, test_auth.db for auth schema
# This avoids SQLite self-ATTACH issues when the same file is used as both main and auth.
os.environ.setdefault("PO_DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("PO_DB_AUTH_SCHEMA", "auth")

import pytest

# ── Rate-limit bypass (module-level -- MUST run before any 'main' import) ────
import middleware.rate_limit as _rl_mod

# Replace rate_limit_video with a no-op that always returns a high limit.
_original_rate_limit_video = _rl_mod.rate_limit_video
_rl_mod.rate_limit_video = lambda request="": "1000/hour"


# ── PostgreSQL fallback to SQLite for testing (module-level) ──────────────
# The test environment may not have PostgreSQL running.
# We redirect the auth tables to a local SQLite file.
# Since SQLite doesn't support schemas, every get_db_pg() call executes
# ATTACH DATABASE on the session to emulate the "auth" schema prefix.

import db_pg as _db_pg_mod
from sqlalchemy import text
from models.auth_models import Base as _AuthBase

_original_get_db_pg = _db_pg_mod.get_db_pg

async def _patched_get_db_pg():
    """Replace Postgres get_db_pg with SQLite version that ATTACHES the auth schema."""
    async with _db_pg_mod.AsyncSessionLocal() as session:
        try:
            await session.execute(text("ATTACH DATABASE './test_auth.db' AS auth"))
        except Exception:
            pass
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

_db_pg_mod.get_db_pg = _patched_get_db_pg

_original_init_pg_db = _db_pg_mod.init_pg_db

async def _sqlite_init_pg_db():
    """Initialize auth tables in SQLite (skip PG-specific schema commands)."""
    async with _db_pg_mod.engine.begin() as conn:
        try:
            await conn.execute(text("ATTACH DATABASE './test_auth.db' AS auth"))
        except Exception:
            pass
        await conn.run_sync(_AuthBase.metadata.create_all)

_db_pg_mod.init_pg_db = _sqlite_init_pg_db

import main as _main_mod
_main_mod.init_pg_db = _sqlite_init_pg_db


# ── Session-scoped fixtures ──────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _init_db_once():
    """Initialize the test database once for the entire test session.

    Uses asyncio.run() at session scope so it only creates one event loop
    instead of one per test module (which causes hangs).
    """
    from db import init_db
    asyncio.run(init_db())
    # Also init auth tables so tests that create inline TestClient work.
    asyncio.run(_sqlite_init_pg_db())
