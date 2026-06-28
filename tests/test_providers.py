"""Tests for providers: registry, circuit breaker, fallback provider."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from providers.registry import ProviderRegistry, ProviderSpec
from providers.fallback_provider import CircuitBreaker, FallbackProvider


class TestProviderSpec:
    def test_minimal_spec(self):
        spec = ProviderSpec(name="test", provider_type="llm")
        assert spec.name == "test"
        assert spec.provider_type == "llm"
        assert not spec.is_gateway
        assert not spec.is_local

    def test_full_spec(self):
        spec = ProviderSpec(
            name="openai", provider_type="llm",
            display_name="OpenAI", base_url="https://api.openai.com/v1",
            env_key="PO_OPENAI_API_KEY",
            keywords=["gpt-", "o1", "o3"],
            is_gateway=False, default_tier=1,
        )
        assert spec.keywords == ["gpt-", "o1", "o3"]
        assert spec.api_key_from_env == ""

    def test_api_key_from_env(self):
        os.environ["PO_TEST_KEY"] = "sk-test123"
        spec = ProviderSpec(name="test", provider_type="llm", env_key="PO_TEST_KEY")
        assert spec.api_key_from_env == "sk-test123"
        del os.environ["PO_TEST_KEY"]


class TestRegistry:
    def setup_method(self):
        ProviderRegistry._initialized = False
        ProviderRegistry._providers = {}

    def test_init_creates_builtins(self):
        ProviderRegistry.init()
        assert len(ProviderRegistry._providers) >= 7

    def test_get_existing(self):
        ProviderRegistry.init()
        spec = ProviderRegistry.get("openai")
        assert spec is not None
        assert spec.name == "openai"
        assert spec.provider_type == "llm"

    def test_get_nonexistent(self):
        ProviderRegistry.init()
        spec = ProviderRegistry.get("nonexistent")
        assert spec is None

    def test_match_model_gpt(self):
        ProviderRegistry.init()
        spec = ProviderRegistry.match_model("gpt-4o")
        assert spec is not None
        assert spec.name == "openai"

    def test_match_model_doubao(self):
        ProviderRegistry.init()
        spec = ProviderRegistry.match_model("ep-2025-01-01")
        assert spec is not None
        assert spec.name == "doubao"

    def test_match_model_no_match(self):
        ProviderRegistry.init()
        spec = ProviderRegistry.match_model("unknown-model-xyz")
        assert spec is None

    def test_list_by_type_video(self):
        ProviderRegistry.init()
        videos = ProviderRegistry.list_by_type("video")
        assert len(videos) >= 2
        assert all(v.provider_type == "video" for v in videos)

    def test_list_by_type_image(self):
        ProviderRegistry.init()
        images = ProviderRegistry.list_by_type("image")
        assert len(images) >= 2

    def test_register_new(self):
        ProviderRegistry.init()
        spec = ProviderSpec(name="custom", provider_type="llm",
                            keywords=["custom-"])
        ProviderRegistry.register(spec)
        assert ProviderRegistry.get("custom") is not None

    def test_init_no_duplicate(self):
        ProviderRegistry.init()
        count = len(ProviderRegistry._providers)
        ProviderRegistry.init()  # Should be no-op
        assert len(ProviderRegistry._providers) == count

    def test_list_all(self):
        ProviderRegistry.init()
        all_providers = ProviderRegistry.list_all()
        assert len(all_providers) >= 7


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state("openai") == "closed"

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        cb.record_failure("openai")
        assert cb.state("openai") == "closed"
        cb.record_failure("openai")
        assert cb.state("openai") == "open"

    def test_is_available_closed(self):
        cb = CircuitBreaker()
        assert cb.is_available("openai") is True

    def test_is_available_open(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("openai")
        assert cb.is_available("openai") is False

    def test_transitions_to_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        cb.record_failure("openai")
        assert cb.state("openai") in ("half-open", "closed")
        assert cb.is_available("openai") is True

    def test_success_resets(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        cb.record_failure("openai")
        cb.record_success("openai")
        assert cb.state("openai") == "closed"
        assert cb._failures.get("openai", 0) == 0

    def test_reset_manually(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("openai")
        cb.reset("openai")
        assert cb.state("openai") == "closed"
        assert cb.is_available("openai") is True

    def test_multiple_providers_independent(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("openai")
        assert cb.is_available("openai") is False
        assert cb.is_available("doubao") is True


class TestFallbackProvider:
    def test_single_provider_success(self):
        cb = CircuitBreaker()
        fp = FallbackProvider(cb)

        with patch.object(fp, "_call_single", new_callable=AsyncMock) as mock:
            mock.return_value = "success"
            result = asyncio_run(fp.call(["openai"], "sys", "user"))
            assert result == "success"

    def test_fallback_on_failure(self):
        cb = CircuitBreaker()
        fp = FallbackProvider(cb)

        with patch.object(fp, "_call_single", new_callable=AsyncMock) as mock:
            mock.side_effect = [ValueError("fail"), "success"]
            result = asyncio_run(fp.call(["openai", "doubao"], "sys", "user"))
            assert result == "success"
            assert mock.call_count == 2

    def test_all_providers_fail(self):
        cb = CircuitBreaker()
        fp = FallbackProvider(cb)

        with patch.object(fp, "_call_single", new_callable=AsyncMock) as mock:
            mock.side_effect = ValueError("fail")
            with pytest.raises(RuntimeError, match="All 3 providers failed"):
                asyncio_run(fp.call(["a", "b", "c"], "sys", "user"))

    def test_skips_open_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        cb.record_failure("openai")
        fp = FallbackProvider(cb)

        with patch.object(fp, "_call_single", new_callable=AsyncMock) as mock:
            mock.return_value = "ok"
            # openai should be skipped, doubao called
            fallbacks_called = []

            def track(provider, error):
                fallbacks_called.append(provider)

            result = asyncio_run(fp.call(
                ["openai", "doubao"], "sys", "user",
                on_fallback=track,
            ))
            assert result == "ok"
            assert "openai" in fallbacks_called
            assert mock.call_count == 1  # Only doubao

    def test_circuit_records_failure(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        fp = FallbackProvider(cb)

        with patch.object(fp, "_call_single", new_callable=AsyncMock) as mock:
            mock.side_effect = ValueError("fail")
            with pytest.raises(RuntimeError):
                asyncio_run(fp.call(["openai"], "sys", "user"))
            assert cb.state("openai") == "open"

    def test_circuit_records_success(self):
        cb = CircuitBreaker()
        fp = FallbackProvider(cb)

        with patch.object(fp, "_call_single", new_callable=AsyncMock) as mock:
            mock.return_value = "ok"
            asyncio_run(fp.call(["openai"], "sys", "user"))
            assert cb.state("openai") == "closed"


def asyncio_run(coro):
    """Helper to run async tests synchronously."""
    import asyncio
    return asyncio.run(coro)