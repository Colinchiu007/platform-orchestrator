"""TrendScope proxy router — exposes trending data through the orchestrator.

Proxies TrendScope's FastAPI (:8001) endpoints so that the orchestrator
(:8000) becomes the single entry point for all platform services.

Endpoints:
- GET /api/trending            → TrendScope GET /api/v1/trending
- GET /api/trending/platforms  → TrendScope GET /api/v1/trending/platforms
- GET /api/trending/{platform} → TrendScope GET /api/v1/trending/{platform}
"""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

TRENDSCOPE_BASE = os.getenv("TRENDSCOPE_API_URL", "http://localhost:8001/api/v1")
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


@router.get("")
async def trending_aggregated(
    category: str = Query("all", description="分类筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """聚合热榜 — 多平台合并的热门话题列表。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{TRENDSCOPE_BASE}/trending",
                params={"category": category, "page": page, "page_size": page_size},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            raise HTTPException(503, detail="TrendScope 服务不可用")
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, detail=e.response.text)


@router.get("/platforms")
async def trending_platforms():
    """支持的平台列表。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.get(f"{TRENDSCOPE_BASE}/trending/platforms")
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            raise HTTPException(503, detail="TrendScope 服务不可用")
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, detail=e.response.text)


@router.get("/{platform}")
async def trending_by_platform(
    platform: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """单平台热榜 — 指定平台的热门话题列表。"""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            resp = await client.get(
                f"{TRENDSCOPE_BASE}/trending/{platform}",
                params={"page": page, "page_size": page_size},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            raise HTTPException(503, detail="TrendScope 服务不可用")
        except httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code, detail=e.response.text)
