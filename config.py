"""Configuration for platform-orchestrator using pydantic-settings.

All values can be overridden via environment variables prefixed with PO_.
Example: PO_SECRET_KEY=my-secret PO_DEBUG=true
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "platform-orchestrator"
    app_version: str = "0.1.0"
    debug: bool = False
    secret_key: str = "change-me-in-production-use-env-var"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 30
    database_url: str = "orchestrator.db"
    feature_gates_path: str = "/srv/projects/feature_gates.yaml"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
    ]

    model_config = {"env_prefix": "PO_", "extra": "allow"}


settings = Settings()
