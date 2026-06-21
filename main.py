"""platform-orchestrator — Thin-shell FastAPI application.

Routes are registered via routers/ modules. Middleware handles
JWT authentication and feature-gate enforcement.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from db import init_db
from routers import aggregator, prompt, publish, splitter, video


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    await init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/health")
    async def health():
        return {"status": "ok", "version": settings.app_version}

    # Feature gates (public endpoint)
    @app.get("/api/features")
    async def list_features():
        from middleware.feature_gate import load_feature_gates
        gates = load_feature_gates()
        return {"features": gates}

    # Register module routers
    app.include_router(aggregator.router, prefix="/api/articles", tags=["articles"])
    app.include_router(splitter.router, prefix="/api/articles", tags=["splitter"])
    app.include_router(prompt.router, prefix="/api/prompts", tags=["prompts"])
    app.include_router(video.router, prefix="/api/jobs", tags=["video"])
    app.include_router(publish.router, prefix="/api/jobs", tags=["publish"])

    return app


app = create_app()
