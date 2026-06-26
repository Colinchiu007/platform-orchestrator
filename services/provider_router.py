"""ProviderRouter — Central provider configuration manager.

Replaces individual settings.xxx_api_key lookups across all services.

Features:
- DB-backed provider config storage (provider_configs + user_api_keys tables)
- AES-GCM encryption (Fernet) for API keys at rest
- Admin CRUD for provider configurations
- User-level API key override
- Tier-based access control
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import aiosqlite
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────


def _derive_fernet_key() -> bytes:
    """Derive a Fernet-compatible key from PO_SECRET_KEY.

    Fernet requires a 32-byte URL-safe base64 key.
    We use SHA-256 to derive a deterministic 32 bytes from any secret.
    """
    import base64
    import hashlib

    secret = os.environ.get("PO_SECRET_KEY", "dev-only-insecure-key-change-me")
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


# Module-level cipher — initialized once.
_FERNET_KEY = _derive_fernet_key()
_CIPHER = Fernet(_FERNET_KEY)


def _encrypt(plaintext: str) -> str:
    """Encrypt a string using AES-GCM (Fernet)."""
    return _CIPHER.encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted string."""
    return _CIPHER.decrypt(ciphertext.encode()).decode()


# ── SQL Statements ────────────────────────────────────────────────────────

SQL_INIT_PROVIDER_CONFIGS = """
    CREATE TABLE IF NOT EXISTS provider_configs (
        id TEXT PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        provider_type TEXT NOT NULL,
        display_name TEXT NOT NULL,
        base_url TEXT NOT NULL,
        api_key_encrypted TEXT NOT NULL,
        models TEXT DEFAULT '[]',
        config TEXT DEFAULT '{}',
        enabled INTEGER DEFAULT 1,
        min_tier INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
"""

SQL_INIT_USER_API_KEYS = """
    CREATE TABLE IF NOT EXISTS user_api_keys (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_uuid TEXT NOT NULL,
        provider_name TEXT NOT NULL,
        api_key_encrypted TEXT NOT NULL,
        base_url TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(user_uuid, provider_name)
    )
"""

SQL_INSERT_PROVIDER = """
    INSERT INTO provider_configs (id, name, provider_type, display_name, base_url,
        api_key_encrypted, models, config, enabled, min_tier)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SQL_UPDATE_PROVIDER = """
    UPDATE provider_configs SET
        provider_type = COALESCE(?, provider_type),
        display_name = COALESCE(?, display_name),
        base_url = COALESCE(?, base_url),
        api_key_encrypted = COALESCE(?, api_key_encrypted),
        models = COALESCE(?, models),
        config = COALESCE(?, config),
        enabled = COALESCE(?, enabled),
        min_tier = COALESCE(?, min_tier),
        updated_at = datetime('now')
    WHERE name = ?
"""

SQL_DELETE_PROVIDER = "DELETE FROM provider_configs WHERE name = ?"

SQL_GET_PROVIDER = "SELECT * FROM provider_configs WHERE name = ?"

SQL_LIST_PROVIDERS = "SELECT * FROM provider_configs ORDER BY name"

SQL_GET_USER_KEY = """
    SELECT api_key_encrypted, base_url FROM user_api_keys
    WHERE user_uuid = ? AND provider_name = ? AND is_active = 1
"""

SQL_UPSERT_USER_KEY = """
    INSERT INTO user_api_keys (user_uuid, provider_name, api_key_encrypted, base_url)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_uuid, provider_name)
    DO UPDATE SET api_key_encrypted = excluded.api_key_encrypted,
                  base_url = excluded.base_url,
                  updated_at = datetime('now')
"""

SQL_DELETE_USER_KEY = """
    UPDATE user_api_keys SET is_active = 0, updated_at = datetime('now')
    WHERE user_uuid = ? AND provider_name = ?
"""


# ── ProviderRouter ────────────────────────────────────────────────────────


class ProviderRouter:
    """Central provider configuration manager.

    Usage:
        router = ProviderRouter()
        router.init_db()

        # Admin: create provider
        router.create({...})

        # Services: get decrypted config
        cfg = router.get("openai")
        client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])

        # User: override key
        router.set_user_key("user-uuid", "openai", "user-sk-key")
        cfg = router.get("openai", user_uuid="user-uuid")
        # Returns user's key if set, else admin key
    """

    def __init__(self, db_path: str = None) -> None:
        self._db_path = db_path or os.environ.get("PO_DB_PATH", "orchestrator.db")
        self._db: Optional[aiosqlite.Connection] = None

    async def init_db(self) -> None:
        """Initialize DB connection and create tables. Called once on app startup."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute(SQL_INIT_PROVIDER_CONFIGS)
        await self._db.execute(SQL_INIT_USER_API_KEYS)
        await self._db.commit()
        logger.info("ProviderRouter tables initialized")

    async def close(self) -> None:
        """Close the DB connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ── Public API ──────────────────────────────────────────────────────

    async def create(self, data: dict) -> dict:
        """Create a new provider config. Returns the created provider.

        Args:
            data: dict with keys: name, provider_type, display_name, base_url,
                  api_key, models (list), min_tier (int), enabled (bool),
                  config (dict)
        """
        import uuid

        encrypted_key = _encrypt(data["api_key"])
        models_json = json.dumps(data.get("models", []), ensure_ascii=False)
        config_json = json.dumps(data.get("config", {}), ensure_ascii=False)

        await self._db.execute(
            SQL_INSERT_PROVIDER,
            (
                str(uuid.uuid4()),
                data["name"],
                data.get("provider_type", "llm"),
                data.get("display_name", data["name"]),
                data["base_url"],
                encrypted_key,
                models_json,
                config_json,
                1 if data.get("enabled", True) else 0,
                data.get("min_tier", 1),
            ),
        )
        await self._db.commit()

        return await self.get(data["name"])

    async def get(self, name: str, user_uuid: str = None) -> Optional[dict]:
        """Get a provider config by name, with optional user key override.

        Args:
            name: Provider name (e.g. "openai", "doubao")
            user_uuid: Optional user UUID — if provided, user's own API key
                       overrides the admin key.

        Returns:
            dict with keys: name, provider_type, display_name, base_url,
            api_key (decrypted), models (list), config (dict), enabled, min_tier
            or None if not found.
        """
        cursor = await self._db.execute(SQL_GET_PROVIDER, (name,))
        row = await cursor.fetchone()

        if row is None:
            return None

        result = dict(row)
        result["api_key"] = _decrypt(result.pop("api_key_encrypted"))
        result["models"] = json.loads(result.get("models", "[]"))
        result["config"] = json.loads(result.get("config", "{}"))
        result["enabled"] = bool(result["enabled"])

        # Check user key override
        if user_uuid:
            user_key = await self._get_user_key(user_uuid, name)
            if user_key:
                result["api_key"] = user_key["api_key_encrypted"]
                if user_key.get("base_url"):
                    result["base_url"] = user_key["base_url"]

        return result

    async def update(self, name: str, data: dict) -> Optional[dict]:
        """Update an existing provider config. Returns updated provider.

        Args:
            name: Provider name to update.
            data: dict with optional keys: provider_type, display_name, base_url,
                  api_key, models, config, enabled, min_tier
        """
        encrypted_key = _encrypt(data["api_key"]) if "api_key" in data else None
        models_json = (
            json.dumps(data["models"], ensure_ascii=False)
            if "models" in data else None
        )
        config_json = (
            json.dumps(data["config"], ensure_ascii=False)
            if "config" in data else None
        )

        await self._db.execute(
            SQL_UPDATE_PROVIDER,
            (
                data.get("provider_type"),
                data.get("display_name"),
                data.get("base_url"),
                encrypted_key,
                models_json,
                config_json,
                data.get("enabled"),
                data.get("min_tier"),
                name,
            ),
        )
        await self._db.commit()

        return await self.get(name)

    async def delete(self, name: str) -> None:
        """Delete a provider config."""
        await self._db.execute(SQL_DELETE_PROVIDER, (name,))
        await self._db.commit()

    async def list_all(self) -> list[dict]:
        """List all provider configs (without decrypted API keys)."""
        cursor = await self._db.execute(SQL_LIST_PROVIDERS)
        rows = await cursor.fetchall()

        results = []
        for row in rows:
            d = dict(row)
            d.pop("api_key_encrypted", None)  # Never expose encrypted key
            d["models"] = json.loads(d.get("models", "[]"))
            d["config"] = json.loads(d.get("config", "{}"))
            d["enabled"] = bool(d["enabled"])
            results.append(d)

        return results

    async def list_available(self, min_tier: int = 1) -> list[dict]:
        """List providers available to a user tier.

        Filters by enabled=True and min_tier <= given tier.
        Never exposes api_key or api_key_encrypted in the list.
        """
        cursor = await self._db.execute(
            "SELECT * FROM provider_configs "
            "WHERE enabled = 1 AND min_tier <= ? "
            "ORDER BY name",
            (min_tier,),
        )
        rows = await cursor.fetchall()

        results = []
        for row in rows:
            d = dict(row)
            d.pop("api_key_encrypted", None)
            d["models"] = json.loads(d.get("models", "[]"))
            d["config"] = json.loads(d.get("config", "{}"))
            results.append(d)

        return results

    async def get_by_type(self, provider_type: str) -> list[dict]:
        """Get all providers of a given type (e.g. 'llm', 'tts', 'image')."""
        cursor = await self._db.execute(
            "SELECT * FROM provider_configs WHERE provider_type = ? ORDER BY name",
            (provider_type,),
        )
        rows = await cursor.fetchall()

        results = []
        for row in rows:
            d = dict(row)
            d["api_key"] = _decrypt(d.pop("api_key_encrypted"))
            d["models"] = json.loads(d.get("models", "[]"))
            d["config"] = json.loads(d.get("config", "{}"))
            d["enabled"] = bool(d["enabled"])
            results.append(d)

        return results

    # ── User API Key Methods ────────────────────────────────────────────

    async def set_user_key(
        self, user_uuid: str, provider_name: str, api_key: str,
        base_url: str = None,
    ) -> None:
        """Set a user's own API key for a provider.

        This key overrides the admin-configured key for this user.
        """
        encrypted_key = _encrypt(api_key)

        await self._db.execute(
            SQL_UPSERT_USER_KEY,
            (user_uuid, provider_name, encrypted_key, base_url),
        )
        await self._db.commit()

    async def delete_user_key(self, user_uuid: str, provider_name: str) -> None:
        """Remove a user's API key override (soft delete)."""
        await self._db.execute(SQL_DELETE_USER_KEY, (user_uuid, provider_name))
        await self._db.commit()

    # ── Internal Methods ────────────────────────────────────────────────

    async def _get_user_key(
        self, user_uuid: str, provider_name: str,
    ) -> Optional[dict]:
        """Get user's API key override (decrypted)."""
        cursor = await self._db.execute(SQL_GET_USER_KEY, (user_uuid, provider_name))
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "api_key_encrypted": _decrypt(row[0]),
            "base_url": row[1],
        }

    async def _init_tables(self) -> None:
        """Internal: init tables for testing."""
        await self._db.execute(SQL_INIT_PROVIDER_CONFIGS)
        await self._db.execute(SQL_INIT_USER_API_KEYS)
        await self._db.commit()

    async def _fetch_tables(self) -> list[str]:
        """Internal: list table names for testing."""
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def _fetch_raw(self, name: str) -> Optional[dict]:
        """Internal: fetch raw DB row (including encrypted key) for testing."""
        cursor = await self._db.execute(SQL_GET_PROVIDER, (name,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _fetch_user_key_raw(
        self, user_uuid: str, provider_name: str,
    ) -> Optional[str]:
        """Internal: fetch raw user key (encrypted) for testing."""
        cursor = await self._db.execute(
            SQL_GET_USER_KEY, (user_uuid, provider_name),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


# ── Singleton ─────────────────────────────────────────────────────────────

_router: Optional[ProviderRouter] = None


def get_router() -> ProviderRouter:
    """Get or create the singleton ProviderRouter instance."""
    global _router
    if _router is None:
        _router = ProviderRouter()
    return _router