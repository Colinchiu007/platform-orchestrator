"""Configuration for platform-orchestrator using pydantic-settings.

All values can be overridden via environment variables prefixed with PO_.
Example: PO_SECRET_KEY=my-secret PO_DEBUG=true

Phase B: PostgreSQL support added — defaults to PG shared with trendscope,
falls back to SQLite for local development with aiosqlite.
"""

from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── App ─────────────────────────────────────────────────────────────
    app_name: str = "platform-orchestrator"
    app_version: str = "0.3.0"
    debug: bool = False

    # ── Auth ────────────────────────────────────────────────────────────
    secret_key: str = ""  # must set PO_SECRET_KEY in environment
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30

    # ── Database (Phase B: PostgreSQL primary, SQLite fallback) ─────────
    database_url: str = (
        "postgresql+asyncpg://trendscope:trendscope_dev@localhost:5432/tendscope"
    )
    # Schema for auth tables within the shared PG instance
    db_auth_schema: str = "auth"

    # ── LLM / AI ────────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"

    # ── AI Service API Keys ──────────────────────────────────────────────
    doubao_api_key: str = ""
    minimax_api_key: str = ""
    kling_api_key: str = ""
    sensenova_api_key: str = ""
    jimeng_api_key: str = ""
    vidu_api_key: str = ""

    # ── Publishing ──────────────────────────────────────────────────────
    wechat_appid: str = ""
    wechat_appsecret: str = ""

    # ── Webhook ──────────────────────────────────────────────────────────
    webhook_secret: str = "dev-webhook-secret"

    # ── Integration API Key ────────────────────────────────────────────
    # Used by Story2Video (and other internal services) to call
    # orchestrator endpoints without full JWT auth.
    api_key: str = ""
    api_key_user_id: str = "api-story2video"

    # ── Feature Gates ───────────────────────────────────────────────────
    feature_gates_path: str = "D:/Data/projects/feature_gates.yaml"

    # ── CORS ────────────────────────────────────────────────────────────
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
    ]

    model_config = {"env_prefix": "PO_", "extra": "allow"}

    @model_validator(mode="after")
    def _validate_secret_key(self):
        if not self.secret_key:
            raise ValueError(
                "PO_SECRET_KEY environment variable is not set. "
                "Set a strong random key before starting the server."
            )
        return self


# Module-level settings instance
settings = Settings()
