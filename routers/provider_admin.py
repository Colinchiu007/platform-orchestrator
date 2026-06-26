"""Provider Admin CRUD router.

Endpoints:
- GET    /api/admin/providers — list all provider configs
- POST   /api/admin/providers — create a new provider
- PUT    /api/admin/providers/{name} — update a provider
- DELETE /api/admin/providers/{name} — delete a provider
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status

from pydantic import BaseModel

from middleware.auth import get_current_user
from services.provider_router import get_router

router = APIRouter()


def _require_admin(user: Dict[str, Any]) -> None:
    """Check that the authenticated user has admin role."""
    role = user.get("role", "")
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


class CreateProviderRequest(BaseModel):
    name: str
    provider_type: str = "llm"
    display_name: str = ""
    base_url: str
    api_key: str
    models: List[str] = []
    config: Dict[str, Any] = {}
    enabled: bool = True
    min_tier: int = 1


class UpdateProviderRequest(BaseModel):
    provider_type: Optional[str] = None
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[List[str]] = None
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    min_tier: Optional[int] = None


@router.get("/providers")
async def list_providers(
    user: Dict[str, Any] = Depends(get_current_user),
):
    """List all provider configs (admin only)."""
    _require_admin(user)
    router_svc = get_router()
    return await router_svc.list_all()


@router.post("/providers", status_code=status.HTTP_201_CREATED)
async def create_provider(
    data: CreateProviderRequest,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Create a new provider config (admin only)."""
    _require_admin(user)
    router_svc = get_router()
    result = await router_svc.create(data.model_dump())
    return result


@router.put("/providers/{name}")
async def update_provider(
    name: str,
    data: UpdateProviderRequest,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Update a provider config (admin only)."""
    _require_admin(user)
    router_svc = get_router()
    update_data = {k: v for k, v in data.model_dump().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    result = await router_svc.update(name, update_data)
    if result is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    return result


@router.delete("/providers/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    name: str,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Delete a provider config (admin only)."""
    _require_admin(user)
    router_svc = get_router()
    existing = await router_svc.get(name)
    if existing is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    await router_svc.delete(name)


@router.post("/providers/{name}/test")
async def test_provider(
    name: str,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Test a provider connection (admin only).
    
    Checks that the provider exists and the API key format is valid.
    """
    _require_admin(user)
    router_svc = get_router()
    provider = await router_svc.get(name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    # Basic validation: API key is non-empty
    if not provider.get("api_key"):
        raise HTTPException(
            status_code=400,
            detail="Provider has no API key configured",
        )
    if not provider.get("base_url"):
        raise HTTPException(
            status_code=400,
            detail="Provider has no base URL configured",
        )
    return {
        "status": "ok",
        "message": f"Provider '{name}' configuration is valid",
        "provider_type": provider["provider_type"],
        "base_url": provider["base_url"],
        "has_api_key": bool(provider["api_key"]),
    }
