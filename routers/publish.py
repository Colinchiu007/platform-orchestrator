"""Multi-platform publish router (Phase 0 stub).

Future endpoints:
- POST /api/jobs/publish — create publish task
- GET /api/jobs/publish/{id} — get publish status
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/publish")
async def create_publish_task():
    return {
        "message": "Multi-platform publish — implementation in Phase 3",
        "status": "pending",
    }


@router.get("/publish/{task_id}")
async def get_publish_status(task_id: str):
    return {
        "message": f"Publish status for {task_id}",
        "task_id": task_id,
        "status": "pending",
    }
