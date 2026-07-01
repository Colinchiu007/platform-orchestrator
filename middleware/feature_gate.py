"""Feature gate middleware.

Controls access to premium features via the @requires_feature decorator.
Gate configuration is loaded from feature_gates.yaml at module load time.
"""

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any, Callable, Dict

import yaml
from fastapi import HTTPException, status

from config import settings

logger = logging.getLogger(__name__)


# ── Hot-reload state ─────────────────────────────────────────────────────
_gates_cache: Dict[str, Any] | None = None
_gates_mtime: float = 0.0
_gates_path: str = settings.feature_gates_path


def load_feature_gates() -> Dict[str, Any]:
    """Load feature gates from YAML configuration."""
    try:
        with open(settings.feature_gates_path, "r") as f:
            gates = yaml.safe_load(f)
        return gates.get("features", {})
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def get_feature_gates() -> Dict[str, Any]:
    """Get feature gates with hot-reload support.

    Checks the YAML file's mtime on each call.  If the file has changed
    since the last load, reloads it transparently.  If the YAML is
    temporarily corrupt (e.g. incomplete write), logs a warning and
    returns the previous (valid) cache — graceful degradation.
    """
    global _gates_cache, _gates_mtime

    try:
        current_mtime = os.path.getmtime(_gates_path)
    except OSError:
        # File disappeared — return whatever we have cached
        return _gates_cache if _gates_cache is not None else {}

    if _gates_cache is not None and current_mtime <= _gates_mtime:
        return _gates_cache

    # mtime changed or first call — try to reload
    try:
        with open(_gates_path, "r") as f:
            gates = yaml.safe_load(f)
        _gates_cache = gates.get("features", {})
        _gates_mtime = current_mtime
        logger.info("Feature gates reloaded (mtime=%.3f)", current_mtime)
    except Exception:
        logger.warning(
            "Failed to reload feature gates from %s, using cached copy",
            _gates_path,
            exc_info=True,
        )
        # Keep existing cache on failure — graceful fallback
        if _gates_cache is None:
            _gates_cache = {}

    return _gates_cache


# Load once at module init
FEATURE_GATES = load_feature_gates()


def requires_feature(feature_name: str):
    """Decorator: enforce that the authenticated user has the required feature tier.

    Usage:
        @router.post("/api/articles/batch-split")
        @requires_feature("split_batch")
        async def batch_split(...): ...
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Extract user from kwargs (FastAPI dependency injection)
            user = kwargs.get("current_user")
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )

            # Get required tier (dynamically reloaded on YAML change)
            gates = get_feature_gates()
            gate = gates.get(feature_name)
            if gate is None:
                # Feature not in gates file — allow by default (open gate)
                return await func(*args, **kwargs)

            required_tier = gate.get("tier", 2)
            user_tier = user.get("tier", 1)

            if user_tier < required_tier:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Feature '{feature_name}' requires premium subscription",
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
