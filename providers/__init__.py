"""Provider registry and fallback chain.

Inspired by nanobot architecture:
    registry.py         — Declarative ProviderSpec registry (model prefix matching)
    fallback_provider.py — Circuit breaker + request-level fallback chain
"""

from providers.registry import ProviderRegistry, ProviderSpec
from providers.fallback_provider import CircuitBreaker, FallbackProvider

__all__ = ["ProviderRegistry", "ProviderSpec", "CircuitBreaker", "FallbackProvider"]
