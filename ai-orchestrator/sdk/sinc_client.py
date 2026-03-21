import os
import uuid
import time
import json
import logging
from typing import Any, Dict, Optional, List

try:
    import httpx
except ImportError:
    httpx = None

class SincClient:
    """
    Official Python Client for SINC Orchestrator 2.0.
    Handles authentication, multi-tenancy, and traceability.
    """
    def __init__(self, base_url: str = None, api_key: str = None, tenant_id: str = "local", timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get("ORCHESTRATOR_URL", "http://localhost:8000")).rstrip("/")
        self.api_key = api_key or os.environ.get("ORCHESTRATOR_API_KEY", "")
        self.tenant_id = tenant_id or os.environ.get("TENANT_ID", "local")
        self.timeout = timeout
        self.log = logging.getLogger("sinc-sdk")

    def _get_headers(self, trace_id: str = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-Id": self.tenant_id,
            "X-Trace-Id": trace_id or f"sdk-{uuid.uuid4().hex[:8]}",
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    async def _request(self, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        if not httpx:
            raise ImportError("httpx is required for SincClient. Install with 'pip install httpx'.")

        url = f"{self.base_url}{path}"
        headers = self._get_headers()

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                self.log.debug(f"Request: {method} {url} trace_id={headers['X-Trace-Id']}")
                response = await client.request(method, url, json=body, headers=headers)
                response.raise_for_status()
                return response.json() if response.content else {}
            except Exception as e:
                self.log.error(f"SincClient Error: {method} {path} -> {e}")
                raise

    # --- Task Management ---
    async def create_task(self, title: str, description: str, agent: str = None, metadata: dict = None) -> Dict[str, Any]:
        payload = {"title": title, "description": description, "agent": agent, "metadata": metadata or {}}
        return await self._request("POST", "/api/v1/tasks", body=payload)

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/v1/tasks/{task_id}")

    async def list_tasks(self) -> List[Dict[str, Any]]:
        res = await self._request("GET", "/api/v1/tasks")
        return res.get("tasks", [])

    # --- Cognitive & Memory ---
    async def search_memory(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        payload = {"query": query, "top_k": top_k}
        return await self._request("POST", "/api/v1/cognitive/memory/search", body=payload)

    # --- System & Heartbeat ---
    async def run_heartbeat(self, component: str) -> Dict[str, Any]:
        """Trigger a specific internal workflow (observer, scheduler, etc)."""
        valid_components = {"observer", "scheduler", "readiness", "external-bridge"}
        if component not in valid_components:
            raise ValueError(f"Invalid component: {component}")
        return await self._request("POST", f"/{component}/run")

    async def get_capabilities(self) -> Dict[str, Any]:
        return await self._request("GET", "/api/v1/system/capabilities")
