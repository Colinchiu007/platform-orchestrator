"""Sentence splitting router (Phase 0 stub).

Future endpoints:
- POST /api/articles/{id}/split — split article into scenes + subtitles
- GET /api/articles/{id}/split — get split results
"""

from fastapi import APIRouter

router = APIRouter()


@router.post("/{article_id}/split")
async def split_article(article_id: str):
    return {
        "message": f"Split requested for article {article_id}",
        "status": "pending",
        "article_id": article_id,
    }
