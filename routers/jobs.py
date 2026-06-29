"""Unified jobs router — list, detail, retry for all job types.

Endpoints:
- GET  /api/jobs/ — list all jobs for current user
- GET  /api/jobs/{job_id} — get any job detail by ID
- POST /api/jobs/{job_id}/retry — retry a failed job
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from db import get_db
from middleware.auth import get_current_user
from middleware.feature_gate import requires_feature

logger = logging.getLogger(__name__)

router = APIRouter()
@router.get("/stats")
async def job_stats(
    days: int = Query(default=7, ge=1, le=90, description="Number of days of history to return"),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Daily job statistics for the current user.

    Returns per-day aggregation (how many jobs in each status per day
    for the last N days) and overall totals across the same period.
    """
    import json as _json

    # Seed the last N days with zeros
    from datetime import datetime, timedelta
    today = datetime.now()
    daily_map: dict[str, dict[str, int]] = {}
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        daily_map[day_str] = {"date": day_str, "pending": 0, "processing": 0, "completed": 0, "failed": 0}

    totals = {"pending": 0, "processing": 0, "completed": 0, "failed": 0}

    # Aggregate jobs by day and status
    try:
        async with db.execute(
            """SELECT date(created_at) as day, status, COUNT(*) as cnt
               FROM jobs WHERE user_id = ?
               AND created_at >= date('now', ?)
               GROUP BY day, status
               ORDER BY day ASC""",
            (current_user["sub"], f"-{days} days"),
        ) as cursor:
            for row in await cursor.fetchall():
                day_str = row["day"]
                status = row["status"]
                cnt = row["cnt"]
                if day_str in daily_map and status in daily_map[day_str]:
                    daily_map[day_str][status] = cnt
                    totals[status] += cnt
    except Exception:
        pass

    return {"daily": list(daily_map.values()), "totals": totals}




@router.get("/")
async def list_all_jobs(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """List all jobs (video, publish, video_publish, story2video) for current user.
    
    Returns combined list of all job types, newest first, up to 50 items.
    Used by unified-frontend /api/jobs/ endpoint.
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


@router.get("/detail/{job_id}")
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


@router.post("/detail/{job_id}/retry")
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


@router.get("/export")
@requires_feature("data_export")
async def export_jobs(
    format: str = Query(default="json", pattern="^(csv|json)$"),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Export user's jobs as CSV or JSON.
    
    Returns all jobs (video, publish, etc.) for the current user
    with appropriate Content-Type for file download.
    """
    async with db.execute(
        """SELECT id, job_type, status, created_at, updated_at
           FROM jobs WHERE user_id = ?
           ORDER BY created_at DESC""",
        (current_user["sub"],),
    ) as cursor:
        rows = await cursor.fetchall()
    
    jobs_list = [dict(r) for r in rows]
    
    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        if jobs_list:
            writer = csv.DictWriter(output, fieldnames=list(jobs_list[0].keys()))
            writer.writeheader()
            writer.writerows(jobs_list)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=jobs_export.csv"},
        )
    
    return {"items": jobs_list, "total": len(jobs_list)}
