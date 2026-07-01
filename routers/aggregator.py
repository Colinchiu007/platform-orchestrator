"""Article content aggregation router.

Endpoints:
- POST /api/articles/fetch — collect article from URL + optional LLM rewrite
- GET  /api/articles/ — list user's articles
- GET  /api/articles/{id} — get article detail
"""

from __future__ import annotations

import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from db import get_db
from middleware.auth import get_current_user
from middleware.feature_gate import requires_feature
from services.collect import collect_url

router = APIRouter()


class FetchRequest(BaseModel):
    url: str = Field(..., description="Article URL to fetch")
    rewrite_style: Optional[str] = Field(
        default=None, description="Rewrite style"
    )
    rewrite_length: Optional[str] = Field(
        default="keep", description="Length strategy"
    )


@router.post("/fetch")
@requires_feature("article_manual_fetch")
async def fetch_article(
    body: FetchRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Fetch article from URL, optionally rewrite with LLM."""
    try:
        collected = await collect_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {str(e)}")

    article_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO articles (id, user_id, source_type, source_url, source_content,
           word_count_original, status)
           VALUES (?, ?, 'url', ?, ?, ?, 'draft')""",
        (
            article_id, current_user["sub"], body.url,
            collected.content, collected.word_count,
        ),
    )
    await db.commit()

    rewrite_result = None
    if body.rewrite_style:
        try:
            from services.rewrite import rewrite_content
            rewrite_result = await rewrite_content(
                content=collected.content,
                style=body.rewrite_style,
                length=body.rewrite_length,
            )
            await db.execute(
                """UPDATE articles SET
                   rewrite_style = ?, rewrite_length = ?,
                   result_content = ?, word_count_result = ?, status = 'rewritten'
                   WHERE id = ?""",
                (rewrite_result.style, rewrite_result.length,
                 rewrite_result.result_content, rewrite_result.word_count, article_id),
            )
            await db.commit()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Rewrite failed: {str(e)}")

    return {
        "article_id": article_id,
        "title": collected.title,
        "author": collected.author,
        "source_url": collected.source_url,
        "word_count_original": collected.word_count,
        "word_count_result": rewrite_result.word_count if rewrite_result else None,
        "content": collected.content,
        "rewritten_content": rewrite_result.result_content if rewrite_result else None,
        "rewrite_style": rewrite_result.style if rewrite_result else None,
        "status": "rewritten" if rewrite_result else "draft",
    }


@router.get("/")
async def list_articles(
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List user's articles with pagination."""
    offset = (page - 1) * page_size
    async with db.execute(
        """SELECT id, source_type, source_url, rewrite_style, rewrite_length,
           word_count_original, word_count_result, status, created_at
           FROM articles WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (current_user["sub"], page_size, offset),
    ) as cursor:
        rows = await cursor.fetchall()

    async with db.execute(
        "SELECT COUNT(*) as total FROM articles WHERE user_id = ?",
        (current_user["sub"],),
    ) as cursor:
        total_row = await cursor.fetchone()

    return {
        "items": [dict(r) for r in rows],
        "total": total_row["total"],
        "page": page,
        "page_size": page_size,
    }


class BatchDeleteRequest(BaseModel):
    article_ids: list[str] = Field(..., description="List of article IDs to delete")


@router.post("/batch-delete")
@requires_feature("batch_operations")
async def batch_delete_articles(
    body: BatchDeleteRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Batch delete articles by IDs.
    
    Only deletes articles belonging to the current user.
    Returns count of successfully deleted articles.
    """
    if not body.article_ids:
        raise HTTPException(status_code=400, detail="No article IDs provided")
    
    deleted = 0
    for aid in body.article_ids:
        await db.execute(
            "DELETE FROM articles WHERE id = ? AND user_id = ?",
            (aid, current_user["sub"]),
        )
        deleted += 1
    await db.commit()
    
    return {"deleted": deleted, "total": len(body.article_ids)}


@router.get("/export")
@requires_feature("data_export")
async def export_articles(
    format: str = Query(default="json", pattern="^(csv|json)$"),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Export user's articles as CSV or JSON.
    
    Returns content with appropriate Content-Type and Content-Disposition headers
    for file download.
    """
    async with db.execute(
        """SELECT id, source_type, source_url, word_count_original, status, created_at
           FROM articles WHERE user_id = ?
           ORDER BY created_at DESC""",
        (current_user["sub"],),
    ) as cursor:
        rows = await cursor.fetchall()
    
    articles = [dict(r) for r in rows]
    
    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        if articles:
            writer = csv.DictWriter(output, fieldnames=list(articles[0].keys()))
            writer.writeheader()
            writer.writerows(articles)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=articles_export.csv"},
        )
    
    return {"items": articles, "total": len(articles)}
@router.get("/{article_id}")
async def get_article(
    article_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get full article detail."""
    async with db.execute(
        "SELECT * FROM articles WHERE id = ? AND user_id = ?",
        (article_id, current_user["sub"]),
    ) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Article not found")

    return dict(row)



