"""Provider User-facing API router.

Endpoints:
- GET    /api/user/providers — list available providers for user's tier
- GET    /api/user/providers/{name} — view a single provider
- PUT    /api/user/providers/{name}/key — set user's own API key
- DELETE /api/user/providers/{name}/key — remove user's API key override
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from middleware.auth import get_current_user
from services.provider_router import get_router

router = APIRouter()


class SetUserKeyRequest(BaseModel):
    api_key: str
    base_url: str | None = None


@router.get("/providers")
async def list_available_providers(
    user: Dict[str, Any] = Depends(get_current_user),
):
    """List providers available to the current user's tier."""
    user_tier = user.get("tier", 1)
    router_svc = get_router()
    return await router_svc.list_available(min_tier=user_tier)


@router.get("/providers/{name}")
async def get_provider(
    name: str,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """View a single provider (without admin API key).
    
    Returns provider info with a masked key if no user key is set.
    """
    user_tier = user.get("tier", 1)
    router_svc = get_router()
    
    # Check the provider exists and is accessible to this user
    available = await router_svc.list_available(min_tier=user_tier)
    provider_names = [p["name"] for p in available]
    if name not in provider_names:
        raise HTTPException(status_code=404, detail="Provider not found or not available")
    
    # Get with user key override
    user_uuid = user.get("sub", "")
    result = await router_svc.get(name, user_uuid=user_uuid)
    if result is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    
    # Mask the API key for non-admin response
    if result.get("api_key"):
        key = result["api_key"]
        if len(key) > 8:
            result["api_key"] = key[:4] + "*" * (len(key) - 8) + key[-4:]
        else:
            result["api_key"] = "****"
    
    return result


@router.put("/providers/{name}/key")
async def set_user_provider_key(
    name: str,
    data: SetUserKeyRequest,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Set user's own API key for a provider."""
    user_uuid = user.get("sub", "")
    router_svc = get_router()
    
    # Verify provider exists
    provider = await router_svc.get(name)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    
    await router_svc.set_user_key(user_uuid, name, data.api_key, data.base_url)
    return {"status": "ok", "message": f"API key set for provider '{name}'"}


@router.delete("/providers/{name}/key", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_provider_key(
    name: str,
    user: Dict[str, Any] = Depends(get_current_user),
):
    """Remove user's own API key override for a provider."""
    user_uuid = user.get("sub", "")
    router_svc = get_router()
    await router_svc.delete_user_key(user_uuid, name)
