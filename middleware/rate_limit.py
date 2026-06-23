"""Rate limiting middleware using slowapi.

Provides IP-based rate limiting for public endpoints (login, register)
and user-ID-based rate limiting for authenticated endpoints (video creation).

Usage:
    from middleware.rate_limit import limiter, setup_rate_limiting

    # In FastAPI app factory:
    setup_rate_limiting(app)

    # On individual routes:
    @router.post("/login")
    @limiter.limit("5/minute")
    async def login(request: Request, ...):
        ...
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded


def _client_ip(request: Request) -> str:
    """Extract the real client IP from X-Forwarded-For or fallback.

    Checks X-Forwarded-For first (for proxied / test setups),
    then X-Real-IP, then request.client.host.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _rate_limit_key(request: Request) -> str:
    """Use user ID if authenticated, otherwise client IP.

    The get_current_user dependency sets request.state.user before
    authenticated route handlers run. Public endpoints (login, register)
    don't require auth, so they fall back to IP-based keying.
    """
    user = getattr(request.state, "user", None)
    if user and "sub" in user:
        return f"user:{user['sub']}"
    return _client_ip(request)


limiter = Limiter(key_func=_rate_limit_key)


def rate_limit_video(request: Request) -> str:
    """Return rate-limit string for video creation based on user tier.

    - Basic tier (tier 1): 10 per day
    - Pro tier (tier 2+): 50 per day
    """
    user = getattr(request.state, "user", None)
    if user and user.get("tier", 1) >= 2:
        return "50/day"
    return "10/day"


def setup_rate_limiting(app: FastAPI) -> None:
    """Enable rate limiting on the FastAPI application.

    Call this during app creation, before registering routers.
    """
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def reset_rate_limits() -> None:
    """Reset all rate-limit counters — used for test isolation.

    Calls the MemoryStorage.reset() method to clear all stored keys.
    """
    limiter._storage.reset()
