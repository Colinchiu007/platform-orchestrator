"""Sentence splitting router — delegates to smart-sentence-splitter.

Endpoints:
- POST /api/articles/{id}/split — split article text into scenes + subtitles
- GET  /api/articles/{id}/split — get existing split result from DB
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from db import get_db
from middleware.auth import get_current_user
from middleware.feature_gate import requires_feature
from splitter import SmartSentenceSplitter

from shared_models import SplitResult

router = APIRouter()

# Module-level instance — initialized once, reused across requests (stateless)
_splitter = SmartSentenceSplitter({"mode": "balanced"})


@router.post("/{article_id}/split")
@requires_feature("split_single")
async def split_article(
    article_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Split article text into structured scenes and subtitles.

    Expects the article content to be in the database.
    Returns SplitResult with sentences[], scenes[], subtitles[].
    """
    # Fetch article from DB
    sql = (
        "SELECT source_content, result_content "
        "FROM articles WHERE id = ? AND user_id = ?"
    )
    async with db.execute(sql, (article_id, current_user["sub"]),) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Article not found")

    text = row["result_content"] or row["source_content"] or ""

    if not text.strip():
        raise HTTPException(status_code=400, detail="Article has no content to split")

    # Call splitter
    result: SplitResult = _splitter.split(text)

    # Store result in DB as JSON
    import json

    sql = (
        "INSERT OR REPLACE INTO splits "
        "(article_id, result_json, tier_used, "
        "total_scenes, total_duration, created_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))"
    )
    await db.execute(
        sql,
        (
            article_id,
            json.dumps(result.to_dict(), ensure_ascii=False),
            result.tier_used,
            result.total_scenes,
            result.total_duration,
        ),
    )
    await db.commit()

    return {
        "article_id": article_id,
        "language": result.language,
        "tier_used": result.tier_used,
        "total_scenes": result.total_scenes,
        "total_duration": result.total_duration,
        "total_words": result.total_words,
        "scenes": [
            {
                "segment_id": s.segment_id,
                "text": s.text,
                "estimated_duration": s.estimated_duration,
                "target_words": s.target_words,
                "subtitle_count": len(s.subtitles),
                "subtitles": [
                    {
                        "text": sub.text,
                        "start_time": sub.start_time,
                        "duration": sub.duration,
                        "display_order": sub.display_order,
                    }
                    for sub in s.subtitles
                ],
            }
            for s in result.scenes
        ],
        "sentences": [
            {
                "index": s.index,
                "text": s.text,
                "char_count": s.char_count,
                "language": s.language,
                "tier": s.tier,
            }
            for s in result.sentences
        ],
    }


@router.get("/{article_id}/split")
async def get_split_result(
    article_id: str,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Retrieve existing split result from database."""
    sql = (
        "SELECT result_json, tier_used, total_scenes, total_duration "
        "FROM splits WHERE article_id = ?"
    )
    async with db.execute(sql, (article_id,),) as cursor:
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Split result not found")

    import json

    return {
        "article_id": article_id,
        "tier_used": row["tier_used"],
        "total_scenes": row["total_scenes"],
        "total_duration": row["total_duration"],
        "result": json.loads(row["result_json"]),
    }
