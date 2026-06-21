"""Web frontend router — serves Jinja2 templates with HTMX."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader

from db import get_db
from middleware.auth import get_current_user, decode_token

router = APIRouter()
_env = Environment(loader=FileSystemLoader("templates"), auto_reload=True)


def _page(name: str, **kwargs) -> HTMLResponse:
    return HTMLResponse(_env.get_template(name).render(**kwargs))


def _get_user_from_cookie(request: Request) -> dict | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_token(token)
        return {"username": payload.get("username", ""), "sub": payload.get("sub", "")}
    except Exception:
        return None


def _require_web_auth(request: Request):
    user = _get_user_from_cookie(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return user


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _get_user_from_cookie(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return _page("login.html", user=None)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return _page("login.html", user=None)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db=Depends(get_db)):
    user = _require_web_auth(request)
    if isinstance(user, RedirectResponse):
        return user

    async with db.execute("SELECT COUNT(*) as c FROM articles WHERE user_id = ?", (user["sub"],)) as c:
        r = await c.fetchone(); article_count = r["c"] if r else 0
    async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE user_id = ? AND job_type='video'", (user["sub"],)) as c:
        r = await c.fetchone(); video_count = r["c"] if r else 0
    async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE user_id = ? AND job_type='publish'", (user["sub"],)) as c:
        r = await c.fetchone(); publish_count = r["c"] if r else 0

    return _page("dashboard.html", user=user, stats={
        "articles": article_count, "videos": video_count, "publishes": publish_count,
    })


@router.get("/articles", response_class=HTMLResponse)
async def articles_page(request: Request):
    user = _require_web_auth(request)
    if isinstance(user, RedirectResponse):
        return user
    return _page("articles.html", user=user)


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, type: str = "video"):
    user = _require_web_auth(request)
    if isinstance(user, RedirectResponse):
        return user
    return _page("jobs.html", user=user, type=type)


@router.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie("access_token")
    return response
