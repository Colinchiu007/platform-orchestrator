"""Middleware package for platform-orchestrator.

- auth.py: JWT authentication middleware (get_current_user dependency)
- feature_gate.py: Feature gate enforcement (@requires_feature decorator)
"""

from middleware.auth import create_access_token, decode_token, get_current_user
from middleware.feature_gate import requires_feature

__all__ = [
    "create_access_token",
    "decode_token",
    "get_current_user",
    "requires_feature",
]
