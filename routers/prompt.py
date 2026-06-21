"""Prompt optimization router (Phase 0 stub).

Future endpoints:
- POST /api/prompts/optimize — optimize a single prompt
- POST /api/prompts/classify — classify prompt style
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/optimize")
async def optimize_prompt():
    return {
        "message": "Prompt optimization — implementation in Phase 2",
        "status": "pending",
    }


@router.post("/classify")
async def classify_prompt():
    return {
        "message": "Prompt classification — implementation in Phase 2",
        "status": "pending",
    }
