"""Tests for ProviderRouter service — TDD.

Covers:
- DB table init (provider_configs + user_api_keys)
- CRUD operations for admin providers
- get() with tier filtering and user key override
- API key encryption at rest
- list_available() by user tier
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from services.provider_router import ProviderRouter


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def router(tmp_path):
    """Create a ProviderRouter with a temp file DB and persistent connection."""
    db_path = str(tmp_path / "test_providers.db")
    r = ProviderRouter(db_path=db_path)
    await r.init_db()
    yield r
    await r.close()


def _make_provider(
    name: str = "test-provider",
    provider_type: str = "llm",
    display_name: str = "Test Provider",
    base_url: str = "https://api.test.com/v1",
    api_key: str = "sk-test-key-12345",
    models: list = None,
    min_tier: int = 1,
    enabled: bool = True,
) -> dict:
    return {
        "name": name,
        "provider_type": provider_type,
        "display_name": display_name,
        "base_url": base_url,
        "api_key": api_key,
        "models": models or ["gpt-4o-mini"],
        "min_tier": min_tier,
        "enabled": enabled,
    }


# ── Tests ─────────────────────────────────────────────────────────────────


class TestProviderRouterInit:
    """DB table initialization."""

    @pytest.mark.asyncio
    async def test_init_creates_tables(self, router):
        """init_db() creates provider_configs and user_api_keys tables."""
        tables = await router._fetch_tables()
        assert "provider_configs" in tables
        assert "user_api_keys" in tables


class TestProviderRouterCRUD:
    """Admin CRUD operations."""

    @pytest.mark.asyncio
    async def test_create_provider(self, router):
        """Create a provider config -> stored in DB."""
        p = _make_provider()
        result = await router.create(p)
        assert result["name"] == "test-provider"
        assert result["provider_type"] == "llm"

    @pytest.mark.asyncio
    async def test_create_provider_encrypts_key(self, router):
        """API key is encrypted at rest - raw key not in DB."""
        p = _make_provider(api_key="sk-secret-999")
        result = await router.create(p)
        raw_row = await router._fetch_raw("test-provider")
        assert raw_row is not None
        assert raw_row["api_key_encrypted"] != "sk-secret-999"
        assert raw_row["api_key_encrypted"].startswith("gAAAAA")  # Fernet prefix

    @pytest.mark.asyncio
    async def test_get_provider(self, router):
        """get() returns decrypted provider config."""
        p = _make_provider(name="openai", api_key="sk-real-key")
        await router.create(p)
        result = await router.get("openai")
        assert result["name"] == "openai"
        assert result["api_key"] == "sk-real-key"  # decrypted
        assert result["base_url"] == "https://api.test.com/v1"
        assert result["models"] == ["gpt-4o-mini"]

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, router):
        """get() for unknown provider -> None."""
        result = await router.get("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_provider(self, router):
        """update() modifies an existing provider."""
        p = _make_provider(name="openai", api_key="old-key")
        await router.create(p)

        updated = await router.update("openai", {"api_key": "new-key", "base_url": "https://new.url/v1"})
        assert updated["api_key"] == "new-key"
        assert updated["base_url"] == "https://new.url/v1"

    @pytest.mark.asyncio
    async def test_delete_provider(self, router):
        """delete() removes provider from DB."""
        p = _make_provider(name="openai")
        await router.create(p)
        await router.delete("openai")
        assert await router.get("openai") is None

    @pytest.mark.asyncio
    async def test_list_all_providers(self, router):
        """list_all() returns all providers."""
        await router.create(_make_provider(name="provider-a"))
        await router.create(_make_provider(name="provider-b", api_key="key-b"))
        all_providers = await router.list_all()
        assert len(all_providers) == 2
        names = [p["name"] for p in all_providers]
        assert "provider-a" in names
        assert "provider-b" in names

    @pytest.mark.asyncio
    async def test_list_excludes_api_key(self, router):
        """list_all() should NOT expose api_key in the list response."""
        await router.create(_make_provider(api_key="secret-123"))
        all_providers = await router.list_all()
        assert "api_key" not in all_providers[0]


class TestProviderRouterUserKey:
    """User-level API key override."""

    @pytest.mark.asyncio
    async def test_set_user_key(self, router):
        """set_user_key() stores user's own API key."""
        await router.create(_make_provider(name="openai"))
        await router.set_user_key("user-1", "openai", "user-sk-key")
        result = await router.get("openai", user_uuid="user-1")
        assert result["api_key"] == "user-sk-key"

    @pytest.mark.asyncio
    async def test_get_without_user_key_returns_admin_key(self, router):
        """get() without user key -> admin's key."""
        await router.create(_make_provider(name="openai", api_key="admin-key"))
        result = await router.get("openai", user_uuid="user-1")
        assert result["api_key"] == "admin-key"  # No user key set

    @pytest.mark.asyncio
    async def test_delete_user_key(self, router):
        """delete_user_key() removes user key -> falls back to admin key."""
        await router.create(_make_provider(name="openai", api_key="admin-key"))
        await router.set_user_key("user-1", "openai", "user-key")
        await router.delete_user_key("user-1", "openai")
        result = await router.get("openai", user_uuid="user-1")
        assert result["api_key"] == "admin-key"

    @pytest.mark.asyncio
    async def test_user_key_encrypted_at_rest(self, router):
        """User's API key is also encrypted in DB."""
        await router.create(_make_provider(name="openai"))
        await router.set_user_key("user-1", "openai", "plain-user-key")
        raw = await router._fetch_user_key_raw("user-1", "openai")
        assert raw != "plain-user-key"
        assert raw.startswith("gAAAAA")  # Fernet prefix


class TestProviderRouterAccess:
    """Tier-based access control."""

    @pytest.mark.asyncio
    async def test_list_available_filters_by_tier(self, router):
        """list_available(tier) only returns providers with min_tier <= tier."""
        await router.create(_make_provider(name="free-tier", min_tier=1))
        await router.create(_make_provider(name="pro-tier", min_tier=3))

        free_available = await router.list_available(min_tier=1)
        free_names = [p["name"] for p in free_available]
        assert "free-tier" in free_names
        assert "pro-tier" not in free_names

        pro_available = await router.list_available(min_tier=3)
        pro_names = [p["name"] for p in pro_available]
        assert "free-tier" in pro_names
        assert "pro-tier" in pro_names

    @pytest.mark.asyncio
    async def test_list_available_excludes_disabled(self, router):
        """list_available() excludes disabled providers."""
        await router.create(_make_provider(name="enabled-one", enabled=True))
        await router.create(_make_provider(name="disabled-one", enabled=False))

        available = await router.list_available(min_tier=1)
        names = [p["name"] for p in available]
        assert "enabled-one" in names
        assert "disabled-one" not in names

    @pytest.mark.asyncio
    async def test_list_available_hides_api_key(self, router):
        """User-facing list should never expose api_key."""
        await router.create(_make_provider(api_key="secret-admin-key"))
        available = await router.list_available(min_tier=1)
        assert "api_key" not in available[0]
        assert "api_key_encrypted" not in available[0]


class TestProviderRouterTypes:
    """Provider type filtering."""

    @pytest.mark.asyncio
    async def test_get_by_type(self, router):
        """get_by_type() filters providers by type."""
        await router.create(_make_provider(name="gpt4", provider_type="llm"))
        await router.create(_make_provider(name="doubao", provider_type="tts"))
        await router.create(_make_provider(name="minimax", provider_type="image"))

        llms = await router.get_by_type("llm")
        assert len(llms) == 1
        assert llms[0]["name"] == "gpt4"

        tts = await router.get_by_type("tts")
        assert len(tts) == 1
        assert tts[0]["name"] == "doubao"


# ── Load Balancing Tests ──────────────────────────────────────────────────


class TestRoundRobinState:
    """Test RoundRobinState selection logic."""

    def test_round_robin_cycles(self):
        from services.provider_router import RoundRobinState
        rr = RoundRobinState()
        seq = [rr.next("llm", 3) for _ in range(6)]
        assert seq == [0, 1, 2, 0, 1, 2]

    def test_round_robin_per_type_independent(self):
        from services.provider_router import RoundRobinState
        rr = RoundRobinState()
        llm_seq = [rr.next("llm", 2) for _ in range(4)]
        tts_seq = [rr.next("tts", 4) for _ in range(4)]
        assert llm_seq == [0, 1, 0, 1]
        assert tts_seq == [0, 1, 2, 3]

    def test_round_robin_single_provider(self):
        from services.provider_router import RoundRobinState
        rr = RoundRobinState()
        seq = [rr.next("single", 1) for _ in range(3)]
        assert seq == [0, 0, 0]

    def test_round_robin_zero_count(self):
        from services.provider_router import RoundRobinState
        rr = RoundRobinState()
        # Should not crash with count=0
        seq = [rr.next("empty", 0) for _ in range(3)]
        assert seq == [0, 0, 0]


class TestProviderRouterSelect:
    """Test provider selection with load balancing."""

    @pytest.mark.asyncio
    async def test_select_provider_returns_next(self, router):
        """select_provider() returns a provider round-robin."""
        await router.create(_make_provider(name="llm-a", provider_type="llm"))
        await router.create(_make_provider(name="llm-b", provider_type="llm"))

        p1 = await router.select_provider("llm")
        p2 = await router.select_provider("llm")
        assert p1 is not None
        assert p2 is not None
        # With 2 providers round-robin, should alternate
        assert p1["name"] in ("llm-a", "llm-b")
        assert p2["name"] in ("llm-a", "llm-b")

    @pytest.mark.asyncio
    async def test_select_provider_single_type(self, router):
        """With one provider, always returns that one."""
        await router.create(_make_provider(name="only-llm", provider_type="llm"))
        for _ in range(3):
            p = await router.select_provider("llm")
            assert p["name"] == "only-llm"
            assert "api_key" in p

    @pytest.mark.asyncio
    async def test_select_provider_no_match_returns_none(self, router):
        """No providers of the requested type -> None."""
        await router.create(_make_provider(name="tts-only", provider_type="tts"))
        p = await router.select_provider("image")
        assert p is None

    @pytest.mark.asyncio
    async def test_select_provider_excludes_disabled(self, router):
        """Disabled providers should not be selected."""
        await router.create(_make_provider(name="enabled-llm", provider_type="llm", enabled=True))
        await router.create(_make_provider(name="disabled-llm", provider_type="llm", enabled=False))

        for _ in range(4):
            p = await router.select_provider("llm")
            assert p is not None
            assert p["name"] == "enabled-llm"  # only enabled one

    @pytest.mark.asyncio
    async def test_select_provider_with_circuit_breaker(self, router):
        """Open circuit breaker causes provider to be skipped."""
        from providers.fallback_provider import CircuitBreaker
        await router.create(_make_provider(name="broken-llm", provider_type="llm"))
        await router.create(_make_provider(name="ok-llm", provider_type="llm"))

        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=300)
        cb.record_failure("broken-llm")
        cb.record_failure("broken-llm")  # open circuit

        # Even though round-robin might pick broken-llm, it's skipped
        for _ in range(4):
            p = await router.select_provider("llm", circuit_breaker=cb)
            assert p is not None
            assert p["name"] == "ok-llm"  # only ok one

    @pytest.mark.asyncio
    async def test_select_provider_all_circuit_broken_returns_none(self, router):
        """All providers with open circuits -> None."""
        from providers.fallback_provider import CircuitBreaker
        await router.create(_make_provider(name="broken-a", provider_type="llm"))
        await router.create(_make_provider(name="broken-b", provider_type="llm"))

        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=300)
        cb.record_failure("broken-a")
        cb.record_failure("broken-a")
        cb.record_failure("broken-b")
        cb.record_failure("broken-b")

        p = await router.select_provider("llm", circuit_breaker=cb)
        assert p is None

    @pytest.mark.asyncio
    async def test_get_by_type_with_user_uuid(self, router):
        """get_by_type() respects user key override."""
        await router.create(_make_provider(name="openai", provider_type="llm", api_key="admin-key"))
        await router.set_user_key("user-1", "openai", "user-key")

        providers = await router.get_by_type("llm", user_uuid="user-1")
        assert len(providers) == 1
        assert providers[0]["name"] == "openai"
        assert providers[0]["api_key"] == "user-key"

    @pytest.mark.asyncio
    async def test_get_by_type_mixed_user_visibility(self, router):
        """User key only applies to the provider the user overrode."""
        await router.create(_make_provider(name="openai", provider_type="llm", api_key="admin-key"))
        await router.create(_make_provider(name="doubao", provider_type="llm", api_key="admin-key-2"))
        await router.set_user_key("user-1", "openai", "user-key")

        providers = await router.get_by_type("llm", user_uuid="user-1")
        api_keys = {p["name"]: p["api_key"] for p in providers}
        assert api_keys["openai"] == "user-key"
        assert api_keys["doubao"] == "admin-key-2"
