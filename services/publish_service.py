"""Publishing service — wraps wechat_publisher for orchestrator use.

Adds sys.path entry to find the Multi-Publish python-backend module.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Add Multi-Publish backend to Python path
_MP_BACKEND = Path("/srv/projects/Multi-Publish/packages/python-backend/src")
if str(_MP_BACKEND) not in sys.path:
    sys.path.insert(0, str(_MP_BACKEND))

from wechat_publisher import WechatPublisher, Article, PublishResult  # noqa: E402

from config import settings


@dataclass
class PublishServiceResult:
    success: bool
    platform: str
    publish_id: Optional[str] = None
    article_url: Optional[str] = None
    error: Optional[str] = None


def _get_publisher() -> WechatPublisher:
    """Get or create WechatPublisher instance."""
    appid = settings.wechat_appid
    secret = settings.wechat_appsecret
    if not appid or not secret:
        raise ValueError(
            "WeChat appid/secret not configured. "
            "Set PO_WECHAT_APPID and PO_WECHAT_APPSECRET env vars."
        )
    return WechatPublisher(appid=appid, secret=secret)


async def publish_to_wechat(
    title: str,
    content_html: str,
    cover_image_path: Optional[str] = None,
    author: str = "",
    digest: str = "",
    source_url: str = "",
) -> PublishServiceResult:
    """Publish an article to WeChat MP.

    Args:
        title: Article title.
        content_html: Article body in HTML format.
        cover_image_path: Local path to cover image (will be uploaded).
        author: Author name.
        digest: Article summary.
        source_url: Original source URL ("阅读原文" link).

    Returns:
        PublishServiceResult with publish_id for status polling.
    """
    try:
        pub = _get_publisher()

        # Upload cover image if provided
        thumb_media_id = ""
        if cover_image_path:
            thumb_media_id = pub.upload_image(cover_image_path)

        # Build article
        article = Article(
            title=title,
            content=content_html,
            thumb_media_id=thumb_media_id,
            author=author or "",
            digest=digest or "",
            content_source_url=source_url or "",
        )

        # Publish (async — don't wait)
        result: PublishResult = pub.publish(article, wait_publish=False)

        if result.success:
            return PublishServiceResult(
                success=True,
                platform="wechat_mp",
                publish_id=result.publish_id,
                article_url=result.article_url,
            )
        else:
            return PublishServiceResult(
                success=False,
                platform="wechat_mp",
                error=f"[{result.errcode}] {result.errmsg}",
            )

    except ValueError as e:
        return PublishServiceResult(success=False, platform="wechat_mp", error=str(e))
    except Exception as e:
        return PublishServiceResult(success=False, platform="wechat_mp", error=f"Exception: {str(e)}")
