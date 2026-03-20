"""
dashboard.py
============
Clean template-based router for the SINC AI Dashboard.
Replaces the monolithic 1.5k line inline HTML implementation.
"""

import os
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["dashboard"])

# Setup template directory relative to this file
# This file is in services/streaming/routes/dashboard.py
# Templates are in services/streaming/templates/
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
template_dir = os.path.join(base_dir, "templates")

templates = Jinja2Templates(directory=template_dir)

@router.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """
    Serves the professional Cognitive NOC Dashboard.
    UI logic is now isolated in /templates and /static for peak maintainability.
    """
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"request": request, "title": "SINC | Cognitive NOC Dashboard"},
    )
