"""Shared pytest fixtures for platform-orchestrator tests.

Handles one-time DB initialization and rate-limit bypass to prevent
event-loop conflicts and SQLite lock contention across test modules.

CRITICAL: The rate-limit monkeypatch MUST run at module level (before
any test module imports 'main'), NOT in a fixture, because slowapi
evaluates limit callables at decoration time during import.
"""

from __future__ import annotations

import asyncio

import pytest

# ── Rate-limit bypass (module-level — MUST run before any 'main' import) ────
import middleware.rate_limit as _rl_mod

# Replace rate_limit_video with a no-op that always returns a high limit.
# The original function accepts (request: Request) but slowapi calls it
# via self.__limit_provider() which passes the request implicitly.
_original_rate_limit_video = _rl_mod.rate_limit_video
_rl_mod.rate_limit_video = lambda request="": "1000/hour"


# ── Session-scoped fixtures ──────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _init_db_once():
    """Initialize the test database once for the entire test session.

    Uses asyncio.run() at session scope so it only creates one event loop
    instead of one per test module (which causes hangs).
    """
    from db import init_db
    asyncio.run(init_db())
