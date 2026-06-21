"""Article content aggregation router (Phase 0 stub).

Future endpoints:
- POST /api/articles/fetch — trigger URL fetch + LLM rewrite
- GET /api/articles/ — list articles
- GET /api/articles/{id} — get article detail
- DELETE /api/articles/{id} — delete article
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def list_articles():
    return {"message": "Article list — implementation in Phase 1", "articles": []}


@router.get("/{article_id}")
async def get_article(article_id: str):
    return {"message": f"Article detail for {article_id}", "article_id": article_id}
