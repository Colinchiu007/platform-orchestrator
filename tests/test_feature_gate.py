"""Tests for feature gate hot-reload middleware — TDD verification.

Covers:
- get_feature_gates() YAML loading, mtime-based hot-reload,
  graceful degradation on corrupt/missing files
- @requires_feature decorator tier enforcement (403, 401, open gate)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi import HTTPException

from middleware.feature_gate import get_feature_gates, requires_feature

# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_yaml(path: Path, features: dict) -> None:
    """Write a minimal feature-gate YAML file."""
    path.write_text(yaml.dump({"features": features}))


def _make_mock_endpoint():
    """Return a simple async endpoint for decorator tests."""

    async def endpoint(current_user: dict | None = None) -> dict:
        return {"success": True}

    return endpoint


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def reset_gates():
    """Reset module-level gate cache/mtime between tests for isolation."""
    import middleware.feature_gate as fg

    fg._gates_cache = None
    fg._gates_mtime = 0.0
    yield


# ── Hot-reload: get_feature_gates() ─────────────────────────────────────────


class TestGetFeatureGates:
    """Tests for the hot-reload gate loader."""

    def test_loads_yaml(self, tmp_path: Path, reset_gates):
        """Valid YAML file → features dict returned."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"split_batch": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            gates = get_feature_gates()

        assert gates == {"split_batch": {"tier": 2}}

    def test_mtime_change_triggers_reload(self, tmp_path: Path, reset_gates):
        """Modified YAML mtime → new features returned on next call."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"old_feature": {"tier": 1}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            # First load — populate cache
            gates1 = get_feature_gates()
            assert gates1 == {"old_feature": {"tier": 1}}

            # Modify file content — ensure mtime changes
            time.sleep(0.05)
            _write_yaml(gates_file, {"new_feature": {"tier": 2}})

            # Second call — should detect mtime change and reload
            gates2 = get_feature_gates()
            assert gates2 == {"new_feature": {"tier": 2}}

    def test_corrupt_yaml_returns_cached(self, tmp_path: Path, reset_gates):
        """Corrupt YAML after a valid load → cached gates returned (no crash)."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"split_batch": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            # First load — populate cache
            gates1 = get_feature_gates()
            assert gates1 == {"split_batch": {"tier": 2}}

            # Write corrupt YAML (invalid syntax)
            time.sleep(0.05)
            gates_file.write_text("features:\n  bad_yaml: [unclosed")

            # Second call — should fall back to cached gates
            gates2 = get_feature_gates()
            assert gates2 == {"split_batch": {"tier": 2}}

    def test_file_disappears_returns_cached(self, tmp_path: Path, reset_gates):
        """File deleted after valid load → cached gates returned."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"split_batch": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            # First load — populate cache
            gates1 = get_feature_gates()
            assert gates1 == {"split_batch": {"tier": 2}}

            # Delete file
            gates_file.unlink()

            # Second call — should return cached gates
            gates2 = get_feature_gates()
            assert gates2 == {"split_batch": {"tier": 2}}

    def test_no_file_first_call_returns_empty(self, tmp_path: Path, reset_gates):
        """No YAML file exists on first call → empty dict (no crash)."""
        gates_file = tmp_path / "nonexistent.yaml"

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            gates = get_feature_gates()

        assert gates == {}


# ── Decorator: @requires_feature() ──────────────────────────────────────────


class TestRequiresFeature:
    """Tests for the @requires_feature decorator."""

    def test_blocks_lower_tier(self, tmp_path: Path, reset_gates):
        """User tier < required tier → HTTP 403 Forbidden."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"split_batch": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            decorated = requires_feature("split_batch")(_make_mock_endpoint())
            user = {"sub": "uuid", "username": "test", "tier": 1}

            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(decorated(current_user=user))

            assert exc_info.value.status_code == 403
            assert "split_batch" in exc_info.value.detail

    def test_allows_equal_tier(self, tmp_path: Path, reset_gates):
        """User tier == required tier → endpoint succeeds."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"split_batch": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            decorated = requires_feature("split_batch")(_make_mock_endpoint())
            user = {"sub": "uuid", "username": "test", "tier": 2}

            result = asyncio.run(decorated(current_user=user))

            assert result == {"success": True}

    def test_allows_higher_tier(self, tmp_path: Path, reset_gates):
        """User tier > required tier → endpoint succeeds."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"split_batch": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            decorated = requires_feature("split_batch")(_make_mock_endpoint())
            user = {"sub": "uuid", "username": "test", "tier": 3}

            result = asyncio.run(decorated(current_user=user))

            assert result == {"success": True}

    def test_unknown_feature_allows_by_default(self, tmp_path: Path, reset_gates):
        """Feature not in gates YAML → allow (open gate default)."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"some_other_feature": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            decorated = requires_feature("undefined_feature")(_make_mock_endpoint())
            user = {"sub": "uuid", "username": "test", "tier": 1}

            result = asyncio.run(decorated(current_user=user))

            assert result == {"success": True}

    def test_missing_user_returns_401(self, tmp_path: Path, reset_gates):
        """current_user is None → HTTP 401 Unauthorized."""
        gates_file = tmp_path / "gates.yaml"
        _write_yaml(gates_file, {"split_batch": {"tier": 2}})

        with patch("middleware.feature_gate._gates_path", str(gates_file)):
            decorated = requires_feature("split_batch")(_make_mock_endpoint())

            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(decorated(current_user=None))

            assert exc_info.value.status_code == 401
