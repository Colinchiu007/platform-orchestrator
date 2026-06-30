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
from services.provider_router import get_router
from services.subscription_lifecycle import daily_maintenance


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize databases on startup."""
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
        """Aggregate health check across all pipeline services."""
        import asyncio
        import httpx
        import time

        services = {
            "orchestrator": f"http://127.0.0.1:8000/health",
            "trendscope": "http://127.0.0.1:8001/health",
            "sss": "http://127.0.0.1:8002/health",
        }

        results = {}
        all_ok = True

        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = {}
            for name, url in services.items():
                tasks[name] = asyncio.create_task(_probe_service(client, name, url))

            for name, task in tasks.items():
                try:
                    result = await task
                except Exception as e:
                    result = {"status": "error", "error": str(e)}
                results[name] = result
                if result.get("status") != "ok":
                    all_ok = False

        return {
            "status": "ok" if all_ok else "degraded",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "services": results,
        }


async def _probe_service(client: httpx.AsyncClient, name: str, url: str) -> dict:
    """Probe a single service endpoint and return its status + latency."""
    import time
    start = time.monotonic()
    try:
        resp = await client.get(url)
        latency = round((time.monotonic() - start) * 1000, 1)
        if resp.status_code == 200:
            data = resp.json()
            return {"status": "ok", "latency_ms": latency, "version": data.get("version")}
        return {"status": "error", "latency_ms": latency, "http_status": resp.status_code}
    except httpx.ConnectError:
        latency = round((time.monotonic() - start) * 1000, 1)
        return {"status": "unreachable", "latency_ms": latency}
    except Exception as e:
        latency = round((time.monotonic() - start) * 1000, 1)
        return {"status": "error", "latency_ms": latency, "error": str(e)}

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
