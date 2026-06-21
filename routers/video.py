"""Video generation job router (Phase 0 stub).

Future endpoints:
- POST /api/jobs/video — create video generation job
- GET /api/jobs/video/{id} — get job status/progress
- GET /api/jobs/video/ — list video jobs
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/video")
async def create_video_job():
    return {
        "message": "Video generation — implementation in Phase 2",
        "status": "pending",
    }


@router.get("/video/{job_id}")
async def get_video_job(job_id: str):
    return {
        "message": f"Video job status for {job_id}",
        "job_id": job_id,
        "status": "queued",
    }
