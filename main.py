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
from routers import provider_admin, provider_user, usage, viral
from routers import admin_users, jobs, user_settings
from services.lifecycle import lifecycle
from services.provider_router import get_router
from services.subscription_lifecycle import daily_maintenance


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize databases on startup."""
    lifecycle.init()
    try:
        await init_db()
        await init_pg_db()
        router = get_router()
        await router.init_db()
        await daily_maintenance()
    except Exception:
        import logging
        logging.warning("Non-critical init error, continuing startup")
    yield
    await lifecycle.shutdown()


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
    @app.get("/api/health/all")
    async def health_all():
        """Check health of all downstream services. Returns unified status."""
        import asyncio
        import httpx

        services = {
            "orchestrator": {"url": "http://localhost:8000/health", "timeout": 5},
            "trendscope-api": {"url": "http://localhost:8001/health", "timeout": 5},
            "sss": {"url": "http://localhost:8002/health", "timeout": 5},
            "unified-frontend": {"url": "http://localhost:3000", "timeout": 5},
        }

        async def _check(name: str, cfg: dict) -> dict:
            try:
                async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
                    r = await client.get(cfg["url"])
                    return {
                        "name": name,
                        "status": "ok" if r.status_code < 500 else "error",
                        "http_code": r.status_code,
                        "latency_ms": r.elapsed.total_seconds() * 1000 if hasattr(r, "elapsed") else None,
                    }
            except httpx.ConnectError:
                return {"name": name, "status": "error", "error": "connection_refused"}
            except httpx.TimeoutException:
                return {"name": name, "status": "error", "error": "timeout"}
            except Exception as e:
                return {"name": name, "status": "error", "error": str(e)}

        results = await asyncio.gather(*[_check(n, c) for n, c in services.items()])
        all_ok = all(r["status"] == "ok" for r in results)
        return {
            "status": "ok" if all_ok else "degraded",
            "total": len(results),
            "healthy": sum(1 for r in results if r["status"] == "ok"),
            "services": results,
        }

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
    app.include_router(trending.router, prefix="/api/trending", tags=["trending"])
    app.include_router(dashboard.router)
    app.include_router(provider_admin.router, prefix="/api/admin", tags=["admin"])
    app.include_router(provider_user.router, prefix="/api/user", tags=["user"])
    app.include_router(usage.router, prefix="/api/user", tags=["user"])
    app.include_router(admin_users.router, prefix="/api/admin", tags=["admin"])
    app.include_router(viral.router, prefix="/api/viral", tags=["viral"])
    app.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
    app.include_router(user_settings.router, prefix="/api/settings", tags=["settings"])
    return app

# Module-level app instance for uvicorn (main:app)
app = create_app()
