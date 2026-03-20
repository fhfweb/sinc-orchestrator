"""
services/streaming/routes/system_infra.py
======================================
Real-time infrastructure metrics for the SINC AI Dashboard.
Uses psutil when available; degrades gracefully when the dependency is absent.
"""
import os
from fastapi import APIRouter, Depends, HTTPException
from ..core.auth import get_tenant_id

try:
    import psutil
except ImportError:  # pragma: no cover - exercised in container bootstrap
    psutil = None

router = APIRouter(prefix="/system", tags=["system-infra"])

@router.get("/infra")
async def get_system_infra(tenant_id: str = Depends(get_tenant_id)):
    """
    Returns real-time hardware metrics for the current orchestrator node.
    """
    if psutil is None:
        raise HTTPException(status_code=503, detail="system metrics backend unavailable: psutil is not installed")

    # CPU percent (interval=None for non-blocking if called frequently)
    cpu_pct = psutil.cpu_percent(interval=0.1)
    
    # Memory
    mem = psutil.virtual_memory()
    
    # Disk (root)
    disk = psutil.disk_usage('/')
    
    return {
        "cpu": {
            "percent": cpu_pct,
            "cores": psutil.cpu_count()
        },
        "memory": {
            "total": mem.total,
            "available": mem.available,
            "percent": mem.percent,
            "used": mem.used
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent
        },
        "node": os.uname().nodename if hasattr(os, 'uname') else "windows-node"
    }
