"""Circuit breaker + request-level fallback chain.

Inspired by nanobot FallbackProvider pattern. Wraps LLM API calls with
per-provider circuit breaker (3 failures → 60s cooldown) and
request-level fallback (try provider A, then B, then C).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

import httpx

from providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Per-provider circuit breaker pattern.

    Tracks failures per provider. After failure_threshold consecutive
    failures, the circuit opens for cooldown_seconds. After cooldown,
    enters half-open state — one success resets, one failure re-opens.

    Usage:
        cb = CircuitBreaker()
        if cb.is_available("openai"):
            try:
                result = await call_openai()
                cb.record_success("openai")
            except Exception:
                cb.record_failure("openai")

        # Check state
        state = cb.state("openai")  # "closed" | "open" | "half-open"
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 60) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}
        self._half_open: set[str] = set()

    def record_failure(self, provider: str) -> None:
        """Record a failure for a provider.

        Increments failure count. If threshold reached, opens circuit.
        """
        count = self._failures.get(provider, 0) + 1
        self._failures[provider] = count

        if count >= self._failure_threshold:
            self._cooldown_until[provider] = time.time() + self._cooldown_seconds
            self._half_open.discard(provider)
            logger.warning(
                "Circuit breaker OPEN for %s (%d consecutive failures)",
                provider, count,
            )

    def record_success(self, provider: str) -> None:
        """Record a success for a provider. Resets failure count."""
        self._failures[provider] = 0
        if provider in self._half_open:
            self._half_open.discard(provider)
            logger.info("Circuit breaker CLOSED for %s (recovered)", provider)

    def is_available(self, provider: str) -> bool:
        """Check if a provider is available (circuit not open)."""
        # Check if in cooldown
        until = self._cooldown_until.get(provider, 0)
        if until > time.time():
            return False

        # Transition from open → half-open after cooldown expires
        if provider in self._failures and self._failures.get(provider, 0) >= self._failure_threshold:
            if provider not in self._half_open:
                self._half_open.add(provider)
                logger.info("Circuit breaker HALF-OPEN for %s (cooldown expired)", provider)
            return True

        return True

    def state(self, provider: str) -> str:
        """Get the current circuit state: 'closed' | 'open' | 'half-open'."""
        until = self._cooldown_until.get(provider, 0)
        if until > time.time():
            return "open"
        if provider in self._half_open:
            return "half-open"
        return "closed"

    def reset(self, provider: str) -> None:
        """Manually reset the circuit breaker for a provider."""
        self._failures[provider] = 0
        self._cooldown_until.pop(provider, None)
        self._half_open.discard(provider)


class FallbackProvider:
    """Request-level provider fallback chain.

    Tries providers in order, skipping those with open circuits.
    Falls back to the next provider on failure.

    Usage:
        fp = FallbackProvider(cb)
        result = await fp.call(
            providers=["openai", "doubao", "minimax"],
            system_prompt="...",
            user_content="...",
        )
    """

    def __init__(self, circuit_breaker: Optional[CircuitBreaker] = None) -> None:
        self.cb = circuit_breaker or CircuitBreaker()

    async def call(
        self,
        providers: list[str],
        system_prompt: str,
        user_content: str,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        on_fallback: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Call an LLM with provider fallback.

        Args:
            providers: Ordered list of provider names to try.
            system_prompt: System prompt for the LLM.
            user_content: User message content.
            model: Optional model override. If None, uses the provider's
                   default model from registry.
            max_tokens: Max tokens in response.
            temperature: LLM temperature.
            on_fallback: Optional callback (provider_name, error_message)
                         invoked on each fallback.

        Returns:
            The LLM response text.

        Raises:
            RuntimeError: If all providers failed.
        """
        last_error: Optional[str] = None

        for provider_name in providers:
            if not self.cb.is_available(provider_name):
                logger.info("Skipping %s (circuit open)", provider_name)
                if on_fallback:
                    on_fallback(provider_name, "circuit open")
                continue

            try:
                result = await self._call_single(
                    provider_name, system_prompt, user_content,
                    model=model, max_tokens=max_tokens, temperature=temperature,
                )
                self.cb.record_success(provider_name)
                return result
            except Exception as e:
                self.cb.record_failure(provider_name)
                last_error = str(e)
                logger.warning("Provider %s failed: %s, trying next", provider_name, e)
                if on_fallback:
                    on_fallback(provider_name, str(e))

        raise RuntimeError(
            f"All {len(providers)} providers failed. Last error: {last_error}"
        )


    async def call_with_router(
        self,
        router: Any,
        provider_type: str = "llm",
        system_prompt: str = "",
        user_content: str = "",
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        user_uuid: str = None,
        on_fallback: Optional[Callable[[str, str], None]] = None,
    ) -> str:
        """Auto-select and call a provider, with fallback.

        Uses ProviderRouter.select_provider() with round-robin to pick the
        next available provider. Falls back through all providers of the
        given type on failure. Circuit breaker is used per-provider.

        Args:
            router: A ProviderRouter instance.
            provider_type: Type of provider to use (e.g. "llm", "tts").
            system_prompt: System prompt for the LLM.
            user_content: User message content.
            model: Optional model override.
            max_tokens: Max tokens in response.
            temperature: LLM temperature.
            user_uuid: Optional user UUID for key override.
            on_fallback: Optional callback (provider_name, error_message).

        Returns:
            The LLM response text.

        Raises:
            RuntimeError: If no provider is available or all failed.
        """
        # Try with auto-selection first
        provider = await router.select_provider(
            provider_type=provider_type,
            user_uuid=user_uuid,
            circuit_breaker=self.cb,
        )

        if provider is None:
            raise RuntimeError(f"No available provider for type: {provider_type}")

        # First try: auto-selected provider
        name = provider["name"]
        api_key = provider["api_key"]
        base_url = provider["base_url"]

        try:
            return await self._call_single_provider(
                name=name, api_key=api_key, base_url=base_url,
                system_prompt=system_prompt, user_content=user_content,
                model=model, max_tokens=max_tokens, temperature=temperature,
            )
        except Exception as e:
            self.cb.record_failure(name)
            last_error = str(e)
            logger.warning("Provider %s failed: %s, trying fallbacks", name, e)
            if on_fallback:
                on_fallback(name, str(e))

        # Fallback: try all other providers of the same type
        all_providers = await router.get_by_type(provider_type, user_uuid=user_uuid)
        for p in all_providers:
            p_name = p["name"]
            if p_name == name:
                continue  # skip the one we already tried
            if not self.cb.is_available(p_name):
                continue

            try:
                result = await self._call_single_provider(
                    name=p_name, api_key=p["api_key"],
                    base_url=p["base_url"],
                    system_prompt=system_prompt, user_content=user_content,
                    model=model, max_tokens=max_tokens, temperature=temperature,
                )
                self.cb.record_success(p_name)
                return result
            except Exception as e:
                self.cb.record_failure(p_name)
                last_error = str(e)
                logger.warning("Provider %s failed: %s", p_name, e)
                if on_fallback:
                    on_fallback(p_name, str(e))

        raise RuntimeError(
            f"All {len(all_providers)} providers of type '{provider_type}' failed. "
            f"Last error: {last_error}"
        )

    async def _call_single_provider(
        self,
        name: str,
        api_key: str,
        base_url: str,
        system_prompt: str,
        user_content: str,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> str:
        """Call a single provider with explicit credentials.

        Args:
            name: Provider name (for logging).
            api_key: The API key.
            base_url: The API base URL.
            system_prompt: System prompt for the LLM.
            user_content: User message content.
            model: Optional model override.
            max_tokens: Max tokens in response.
            temperature: LLM temperature.

        Returns:
            The LLM response text.
        """
        actual_model = model or "gpt-4o-mini"
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": actual_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def _call_single(
        self,
        provider_name: str,
        system_prompt: str,
        user_content: str,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> str:
        """Call a single provider via its API.

        Looks up provider spec from the registry to get base_url and
        API key. Constructs an OpenAI-compatible chat completions request.
        """
        spec = ProviderRegistry.get(provider_name)
        if spec is None:
            raise ValueError(f"Unknown provider: {provider_name}")

        api_key = spec.api_key_from_env
        if not api_key:
            raise ValueError(f"No API key configured for {provider_name} (env: {spec.env_key})")

        actual_model = model or (spec.models[0] if spec.models else "gpt-4o-mini")

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{spec.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": actual_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
