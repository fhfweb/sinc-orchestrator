"""
dashboard.py
============
Clean template-based router for the SINC AI Dashboard.
Replaces the monolithic 1.5k line inline HTML implementation.
"""

import os
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["dashboard"])

# Setup template directory relative to this file
# This file is in services/streaming/routes/dashboard.py
# Templates are in services/streaming/templates/
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
template_dir = os.path.join(base_dir, "templates")
vue_dist = os.path.join(base_dir, "static", "dist")

templates = Jinja2Templates(directory=template_dir)

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """
    Serves the professional Cognitive NOC Dashboard (legacy single-file).
    UI logic is now isolated in /templates and /static for peak maintainability.
    """
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "title": "SINC | Cognitive NOC Dashboard"},
    )

@router.get("/noc", response_class=HTMLResponse)
@router.get("/noc/{path:path}", response_class=HTMLResponse)
async def get_vue_app(_request: Request, _path: str = ""):
    """
    Serves the Vue 3 NOC Dashboard SPA. All /noc/* routes are handled
    by Vue Router client-side. Falls back to legacy dashboard if dist not built yet.
    """
    index_path = os.path.join(vue_dist, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    # Fallback: redirect to legacy dashboard
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")
