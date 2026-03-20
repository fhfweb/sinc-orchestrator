import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
from services.otel_setup import current_trace_id, span

log = logging.getLogger("orch.http")

_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=10.0)


def _default_headers(service_name: str, headers: Mapping[str, str] | None) -> dict[str, str]:
    merged = {
        "User-Agent": f"sinc-orchestrator/{service_name}",
        "X-Orchestrator-Service": service_name,
    }
    if headers:
        merged.update({str(k): str(v) for k, v in headers.items() if v is not None})
    return merged


def create_resilient_client(
    *,
    service_name: str = "orchestrator",
    base_url: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | float | None = None,
    limits: httpx.Limits | None = None,
    follow_redirects: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    async def log_request(request: httpx.Request) -> None:
        trace_id = request.headers.get("X-Trace-Id") or current_trace_id() or uuid.uuid4().hex[:12]
        request.headers["X-Trace-Id"] = trace_id
        request.extensions["orch.start_time"] = time.perf_counter()
        request.extensions["orch.trace_id"] = trace_id
        log.debug(
            "http_out trace_id=%s service=%s method=%s url=%s",
            trace_id,
            service_name,
            request.method,
            request.url,
        )

    async def log_response(response: httpx.Response) -> None:
        request = response.request
        started_at = request.extensions.get("orch.start_time")
        elapsed_ms = None
        if isinstance(started_at, (int, float)):
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        with span(
            "http.client.response",
            service_name=service_name,
            trace_id=request.extensions.get("orch.trace_id", ""),
            method=request.method,
            url=str(request.url),
            status_code=response.status_code,
            latency_ms=elapsed_ms if elapsed_ms is not None else "unknown",
        ):
            pass
        log.info(
            "http_in trace_id=%s service=%s status=%s latency_ms=%s method=%s url=%s",
            request.headers.get("X-Trace-Id", "unknown"),
            service_name,
            response.status_code,
            elapsed_ms if elapsed_ms is not None else "unknown",
            request.method,
            request.url,
        )

    merged_headers = _default_headers(service_name, headers)
    return httpx.AsyncClient(
        base_url=base_url or "",
        headers=merged_headers,
        event_hooks={"request": [log_request], "response": [log_response]},
        timeout=timeout or _DEFAULT_TIMEOUT,
        limits=limits or _DEFAULT_LIMITS,
        follow_redirects=follow_redirects,
        transport=transport,
    )


def create_sync_resilient_client(
    *,
    service_name: str = "orchestrator",
    base_url: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | float | None = None,
    limits: httpx.Limits | None = None,
    follow_redirects: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    def log_request(request: httpx.Request) -> None:
        trace_id = request.headers.get("X-Trace-Id") or current_trace_id() or uuid.uuid4().hex[:12]
        request.headers["X-Trace-Id"] = trace_id
        request.extensions["orch.start_time"] = time.perf_counter()
        request.extensions["orch.trace_id"] = trace_id
        log.debug(
            "http_out trace_id=%s service=%s method=%s url=%s",
            trace_id,
            service_name,
            request.method,
            request.url,
        )

    def log_response(response: httpx.Response) -> None:
        request = response.request
        started_at = request.extensions.get("orch.start_time")
        elapsed_ms = None
        if isinstance(started_at, (int, float)):
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
        with span(
            "http.client.response",
            service_name=service_name,
            trace_id=request.extensions.get("orch.trace_id", ""),
            method=request.method,
            url=str(request.url),
            status_code=response.status_code,
            latency_ms=elapsed_ms if elapsed_ms is not None else "unknown",
        ):
            pass
        log.info(
            "http_in trace_id=%s service=%s status=%s latency_ms=%s method=%s url=%s",
            request.headers.get("X-Trace-Id", "unknown"),
            service_name,
            response.status_code,
            elapsed_ms if elapsed_ms is not None else "unknown",
            request.method,
            request.url,
        )

    merged_headers = _default_headers(service_name, headers)
    return httpx.Client(
        base_url=base_url or "",
        headers=merged_headers,
        event_hooks={"request": [log_request], "response": [log_response]},
        timeout=timeout or _DEFAULT_TIMEOUT,
        limits=limits or _DEFAULT_LIMITS,
        follow_redirects=follow_redirects,
        transport=transport,
    )


async def get_resilient_client(
    *,
    service_name: str = "orchestrator",
    base_url: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | float | None = None,
    limits: httpx.Limits | None = None,
    follow_redirects: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return create_resilient_client(
        service_name=service_name,
        base_url=base_url,
        headers=headers,
        timeout=timeout,
        limits=limits,
        follow_redirects=follow_redirects,
        transport=transport,
    )
