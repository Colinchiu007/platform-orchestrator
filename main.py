"""platform-orchestrator — Thin-shell FastAPI application.

Routes are registered via routers/ modules. Middleware handles
JWT authentication and feature-gate enforcement.
Phase B: init_pg_db added for PostgreSQL auth tables.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from db import init_db
from db_pg import init_pg_db
from middleware.rate_limit import setup_rate_limiting
from routers import aggregator, auth, dashboard, payment, prompt, publish, splitter, trending, video, web
from routers import provider_admin, provider_user
from services.provider_router import get_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize databases on startup."""
    await init_db()
    await init_pg_db()
    router = get_router()
    await router.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    setup_rate_limiting(app)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": settings.app_version}

    @app.get("/api/features")
    async def list_features():
        from middleware.feature_gate import load_feature_gates
        gates = load_feature_gates()
        return {"features": gates}

    app.mount("/static", StaticFiles(directory="static"), name="static")
    app.include_router(web.router)
    app.include_router(auth.router)
    app.include_router(payment.router)
    app.include_router(aggregator.router, prefix="/api/articles", tags=["articles"])
    app.include_router(splitter.router, prefix="/api/articles", tags=["splitter"])
    app.include_router(prompt.router, prefix="/api/prompts", tags=["prompts"])
    app.include_router(video.router, prefix="/api/jobs", tags=["video"])
    app.include_router(publish.router, prefix="/api/jobs", tags=["publish"])
    app.