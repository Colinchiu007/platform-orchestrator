"""Unified jobs router — list, detail, retry for all job types.

Endpoints:
- GET  /api/jobs/ — list all jobs for current user
- GET  /api/jobs/{job_id} — get any job detail by ID
- POST /api/jobs/{job_id}/retry — retry a failed job
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from db import get_db
from middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def list_all_jobs(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """List all jobs (video, publish, video_publish, story2video) for current user.
    
    Returns combined list of all job types, newest first, up to 50 items.
    Used by unified-frontend /api/jobs endpoint.
    """
    async with db.execute(
        """SELECT id, job_type, status, created_at, updated_at, input_data
           FROM jobs WHERE user_id = ?
           ORDER BY created_at DESC LIMIT 50""",
        (current_user["sub"],),
    ) as cursor:
        rows = await cursor.fetchall()

    items = []
    for r in rows:
        item = dict(r)
        try:
            item["input_data"] = json.loads(item.get("input_data") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["input_data"] = {}
        items.append(item)

    return {"items": items, "total": len(items)}


@router.get("/{job_id}")
async def get_job_detail(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get any job detail by its ID.
    
    Works across all job types (video, publish, video_publish, etc.).
    Returns full job record with parsed input_data and output_data.
    """
    async with db.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        (job_id, current_user["sub"]),
    ) as cursor:
        job = await cursor.fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "id": job["id"],
        "job_type": job["job_type"],
        "status": job["status"],
        "progress": None,
        "result_url": None,
        "input_data": json.loads(job["input_data"]) if job["input_data"] else {},
        "output_data": json.loads(job["output_data"]) if job["output_data"] else {},
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


@router.post("/{job_id}/retry")
async def retry_job(
    job_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Retry a failed job — reset its status to 'pending'.
    
    Only failed/error jobs can be retried. Resets error field to NULL.
    Returns 400 if job is not in a retryable state.
    """
    async with db.execute(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        (job_id, current_user["sub"]),
    ) as cursor:
        job = await cursor.fetchone()

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job["status"] not in ("failed", "error"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry job in status '{job['status']}'. Only failed/error jobs can be retried.",
        )

    await db.execute(
        """UPDATE jobs
           SET status = 'pending', error = NULL, updated_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    await db.commit()

    return {"status": "pending", "message": "Job queued for retry"}
