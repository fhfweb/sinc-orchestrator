"""
Minimal Python client loop for the canonical control plane.

The old PowerShell autonomous loop is being retired. This worker keeps the
consumer-side loop contract alive by periodically nudging the canonical
streaming runtime to execute observer/scheduler/readiness/bridge ticks.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]
    import urllib.request as urllib_request


ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "").rstrip("/")
ORCHESTRATOR_API_KEY = os.environ.get("ORCHESTRATOR_API_KEY", "")
TENANT_ID = os.environ.get("TENANT_ID", "local")
PROJECT_ID = os.environ.get("PROJECT_ID", "")
INTERVAL_SECONDS = int(os.environ.get("ORCHESTRATOR_LOOP_INTERVAL_SECONDS", "120"))
MAX_CYCLES = int(os.environ.get("ORCHESTRATOR_LOOP_MAX_CYCLES", "0"))
REQUEST_TIMEOUT_S = float(os.environ.get("ORCHESTRATOR_LOOP_REQUEST_TIMEOUT_SECONDS", "30"))

_ENDPOINTS = (
    "/observer/run",
    "/scheduler/run",
    "/readiness/run",
    "/external-bridge/run",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(message: str) -> None:
    print(f"[{_now()}] [client-loop] {message}", flush=True)


def _request(path: str) -> dict:
    if not ORCHESTRATOR_URL:
        raise RuntimeError("ORCHESTRATOR_URL is required")
    if not ORCHESTRATOR_API_KEY:
        raise RuntimeError("ORCHESTRATOR_API_KEY is required")

    headers = {
        "X-Api-Key": ORCHESTRATOR_API_KEY,
        "X-Tenant-ID": TENANT_ID,
        "Content-Type": "application/json",
    }
    payload = {"project_id": PROJECT_ID} if PROJECT_ID else {}

    if httpx is not None:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            response = client.post(
                f"{ORCHESTRATOR_URL}{path}",
                headers=headers,
                json=payload,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"{path} -> {response.status_code}: {response.text[:200]}")
            return response.json() if response.content else {}

    data = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        f"{ORCHESTRATOR_URL}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
        raw = response.read().decode("utf-8") or "{}"
        return json.loads(raw)


def main() -> int:
    cycle = 0
    _log(
        "starting "
        f"url={ORCHESTRATOR_URL or '<unset>'} tenant={TENANT_ID} "
        f"project={PROJECT_ID or '<none>'} interval={INTERVAL_SECONDS}s max_cycles={MAX_CYCLES}"
    )

    while True:
        cycle += 1
        for path in _ENDPOINTS:
            try:
                result = _request(path)
                _log(f"{path} ok status={result.get('status', result.get('ok', 'unknown'))}")
            except Exception as exc:  # pragma: no cover - runtime path
                _log(f"{path} error={exc}")

        if MAX_CYCLES > 0 and cycle >= MAX_CYCLES:
            _log("max_cycles reached, exiting")
            return 0

        time.sleep(max(INTERVAL_SECONDS, 5))


if __name__ == "__main__":
    sys.exit(main())
