"""Declarative ProviderSpec registry — inspired by nanobot providers/registry.py.

Provides known provider metadata (env keys, model prefixes, default URLs)
so that fallback and routing logic can operate without hardcoding.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field


# ── ProviderSpec ──────────────────────────────────────────────────────────────


class ProviderSpec(BaseModel):
    """Declarative specification for a single AI provider.

    Fields:
        name:           Unique short name (e.g. "openai", "doubao")
        provider_type:  "llm" | "video" | "image"
        display_name:   Human-readable name
        base_url:       API base URL
        env_key:        Environment variable name for the API key
        models:         Model name prefixes (e.g. ["gpt-", "o1", "o3"])
        is_gateway:     True if this is a gateway/aggregator
        is_local:       True if this is a local model (Ollama, etc.)
        default_tier:   Default access tier
        config:         Additional configuration as dict
    """

    name: str
    provider_type: str
    display_name: str = ""
    base_url: str = ""
    env_key: str = ""
    models: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    is_gateway: bool = False
    is_local: bool = False
    default_tier: int = 1
    config: dict = Field(default_factory=dict)

    @property
    def api_key_from_env(self) -> str:
        """Read the API key from the environment variable specified by env_key."""
        return os.environ.get(self.env_key, "")


# ── Built-in Providers ───────────────────────────────────────────────────────


_BUILTIN_PROVIDERS: list[dict] = [
    # ── LLM ──
    {
        "name": "openai",
        "provider_type": "llm",
        "display_name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env_key": "PO_OPENAI_API_KEY",
        "keywords": ["gpt-", "o1", "o3"],
    },
    {
        "name": "doubao",
        "provider_type": "llm",
        "display_name": "Doubao (ByteDance)",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "env_key": "PO_DOUBAO_API_KEY",
        "keywords": ["doubao", "ep-"],
    },
    {
        "name": "minimax",
        "provider_type": "llm",
        "display_name": "MiniMax",
        "base_url": "https://api.minimax.chat/v1",
        "env_key": "PO_MINIMAX_API_KEY",
        "keywords": ["minimax"],
    },
    # ── Video ──
    {
        "name": "kling",
        "provider_type": "video",
        "display_name": "Kling (Kuaishou)",
        "base_url": "https://api.klingai.com",
        "env_key": "PO_KLING_API_KEY",
        "keywords": ["kling"],
    },
    {
        "name": "vidu",
        "provider_type": "video",
        "display_name": "Vidu (Shengshu)",
        "base_url": "https://api.vidu.cn/v1",
        "env_key": "PO_VIDU_API_KEY",
        "keywords": ["vidu"],
    },
    # ── Image ──
    {
        "name": "sensenova",
        "provider_type": "image",
        "display_name": "SenseNova (SenseTime)",
        "base_url": "https://api.sensenova.cn/v1",
        "env_key": "PO_SENSENOVA_API_KEY",
        "keywords": ["nova"],
    },
    {
        "name": "jimeng",
        "provider_type": "image",
        "display_name": "Jimeng (ByteDance)",
        "base_url": "https://api.jimeng.io/v1",
        "env_key": "PO_JIMENG_API_KEY",
        "keywords": ["jimeng"],
    },
]


# ── ProviderRegistry ──────────────────────────────────────────────────────────


class ProviderRegistry:
    """Singleton-style registry for ProviderSpec entries.

    Usage:
        ProviderRegistry.init()  # Load built-in providers
        spec = ProviderRegistry.match_model("gpt-4o")
        spec = ProviderRegistry.get("openai")
        providers = ProviderRegistry.list_by_type("video")
    """

    _providers: dict[str, ProviderSpec] = {}
    _initialized: bool = False

    @classmethod
    def init(cls, extras: Optional[list[dict]] = None) -> None:
        """Load built-in providers, plus any extras from DB or config."""
        if cls._initialized:
            return

        for data in _BUILTIN_PROVIDERS:
            spec = ProviderSpec(**data)
            cls._providers[spec.name] = spec

        if extras:
            for data in extras:
                spec = ProviderSpec(**data)
                cls._providers[spec.name] = spec

        cls._initialized = True

    @classmethod
    def get(cls, name: str) -> Optional[ProviderSpec]:
        """Get a provider spec by name."""
        if not cls._initialized:
            cls.init()
        return cls._providers.get(name)

    @classmethod
    def match_model(cls, model_name: str) -> Optional[ProviderSpec]:
        """Find the provider that matches a model name via keyword prefix.

        Checks each provider's keywords list: if any keyword appears at
        the start of model_name, that provider is returned.

        Example:
            ProviderRegistry.match_model("gpt-4o") -> ProviderSpec(openai)
            ProviderRegistry.match_model("ep-2025-01") -> ProviderSpec(doubao)
        """
        if not cls._initialized:
            cls.init()

        lower_model = model_name.lower()
        for spec in cls._providers.values():
            for kw in spec.keywords:
                if lower_model.startswith(kw.lower()):
                    return spec
        return None

    @classmethod
    def list_by_type(cls, provider_type: str) -> list[ProviderSpec]:
        """List all providers of a given type (llm, video, image, etc.)."""
        if not cls._initialized:
            cls.init()
        return [s for s in cls._providers.values() if s.provider_type == provider_type]

    @classmethod
    def register(cls, spec: ProviderSpec) -> None:
        """Register a new provider spec at runtime."""
        cls._providers[spec.name] = spec

    @classmethod
    def list_all(cls) -> list[ProviderSpec]:
        """List all registered providers."""
        if not cls._initialized:
            cls.init()
        return list(cls._providers.values())
