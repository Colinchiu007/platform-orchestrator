"""Feature gate middleware.

Controls access to premium features via the @requires_feature decorator.
Gate configuration is loaded from feature_gates.yaml at module load time.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Dict

import yaml
from fastapi import HTTPException, status

from config import settings


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

            # Get required tier
            gate = FEATURE_GATES.get(feature_name)
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
