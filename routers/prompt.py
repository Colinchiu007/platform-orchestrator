"""Prompt optimization & classification router.

Endpoints:
- POST /optimize — optimize scene text into image-generation prompts
- POST /classify — classify scene text as narrative/descriptive/dialogue/action
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from config import settings
from middleware.auth import get_current_user
from services.prompt_service import optimize_prompt as optimize_prompt_service
from services.rewrite import _call_llm

router = APIRouter()

# ── Request / Response Models ──────────────────────────────────────────────────


class OptimizeRequest(BaseModel):
    scene_text: str = Field(..., min_length=1, description="Scene text to optimize")
    segments: Optional[List[str]] = Field(
        None, description="Optional sub-segments to optimize individually"
    )


class OptimizeResponse(BaseModel):
    prompts: List[str]


class ClassifyRequest(BaseModel):
    scene_text: str = Field(..., min_length=1, description="Scene text to classify")


class ClassifyResponse(BaseModel):
    scene_type: str
    confidence: float


# ── System Prompt ──────────────────────────────────────────────────────────────

CLASSIFY_SYSTEM_PROMPT = """\
You are a scene type classifier. Classify the following scene text into \
one of these categories:
- narrative: Storytelling, plot progression
- descriptive: Detailed description of a scene, character, or object
- dialogue: Conversation between characters
- action: Movement, combat, or dynamic events

Respond with ONLY the category name and nothing else."""


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.post("/optimize", response_model=OptimizeResponse)
async def optimize_prompt(
    request: OptimizeRequest,
    current_user: dict = Depends(get_current_user),
) -> OptimizeResponse:
    """Optimize scene text into image-generation prompts.

    Calls the LLM to transform scene descriptions into high-quality
    prompts suitable for image generation models.
    """
    result = await optimize_prompt_service(
        text=request.scene_text,
        segments=request.segments,
    )
    return OptimizeResponse(prompts=result.prompts)


@router.post("/classify", response_model=ClassifyResponse)
async def classify_prompt(
    request: ClassifyRequest,
    current_user: dict = Depends(get_current_user),
) -> ClassifyResponse:
    """Classify scene text into a narrative/descriptive/dialogue/action type.

    Uses the LLM to determine the scene type and returns a confidence score.
    """
    llm_result = await _call_llm(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        system_prompt=CLASSIFY_SYSTEM_PROMPT,
        user_content=request.scene_text,
    )
    scene_type = llm_result.strip().lower()
    # Confidence is a reasonable default when using the LLM classifier
    confidence = 0.85
    return ClassifyResponse(scene_type=scene_type, confidence=confidence)
