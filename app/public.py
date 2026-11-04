"""Public marketing page, local assets, privacy, and closed-onboarding notice."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app import pages
from app.waitlist import waitlist_routes

LANDING = Path(__file__).parent / "static" / "landing"
ASSETS = {"reference.css", "fonts.css", "landing.css", "b2b.css", "waitlist.js"}


def public_routes(signup_enabled: bool) -> APIRouter:
    router = APIRouter()
    router.include_router(waitlist_routes())

    @router.get("/", response_class=HTMLResponse)
    def home():
        return HTMLResponse((LANDING / "index.html").read_text(encoding="utf-8"))

    @router.get("/b2b", response_class=HTMLResponse)
    def business():
        return HTMLResponse((LANDING / "b2b.html").read_text(encoding="utf-8"))

    @router.get("/pippa-assets/{filename}")
    def asset(filename: str):
        if filename not in ASSETS:
            raise HTTPException(status_code=404)
        return FileResponse(LANDING / filename, headers={"Cache-Control": "public, max-age=3600"})

    @router.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return HTMLResponse(pages.privacy())

    if not signup_enabled:

        @router.get("/signup", response_class=HTMLResponse)
        @router.get("/signin", response_class=HTMLResponse)
        def signup_closed():
            return HTMLResponse(pages.home_closed())

    return router
