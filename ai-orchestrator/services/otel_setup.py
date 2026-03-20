"""
OpenTelemetry Setup — shared module for all orchestrator services
=================================================================
Import and call `configure_otel(service_name)` once at startup.

Exporter priority
-----------------
1. OTLP gRPC  — if OTEL_EXPORTER_OTLP_ENDPOINT is set
2. Console     — fallback (stdout JSON spans, useful in dev)

Environment variables
---------------------
OTEL_SERVICE_NAME              — service name (overrides function arg)
OTEL_EXPORTER_OTLP_ENDPOINT   — e.g. http://otel-collector:4317
OTEL_TRACES_SAMPLER            — "always_on" | "always_off" | "parentbased_traceid_ratio"
OTEL_TRACES_SAMPLER_ARG        — ratio for traceid_ratio (default 1.0)
OTEL_ENABLED                   — "false" to disable entirely (default "true")

Usage
-----
  from services.otel_setup import configure_otel, get_tracer, span

  configure_otel("streaming-server")

  tracer = get_tracer()

  with span("my.operation"):
      do_something()

  # Or use the tracer directly:
  with tracer.start_as_current_span("db.query") as s:
      s.set_attribute("db.statement", sql)
      result = run_query()
"""

from __future__ import annotations
from services.streaming.core.config import env_get

import os
from contextlib import contextmanager, nullcontext
from typing import Any

# ── OpenTelemetry (graceful no-op when not installed) ─────────────────────────

_tracer_holder: dict[str, Any] = {"v": None, "provider": None, "configured": False}

_ENABLED = env_get("OTEL_ENABLED", default="true").lower() not in ("false", "0", "no")


def _parse_otel_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for part in str(raw or "").split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip():
            headers[key.strip()] = value.strip()
    return headers


def configure_otel(service_name: str = "") -> None:
    """
    Initialise the TracerProvider.  Safe to call multiple times — idempotent.
    """
    if _tracer_holder["configured"] or not _ENABLED:
        return

    svc = env_get("OTEL_SERVICE_NAME", default=service_name) or service_name or "orchestrator"

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({"service.name": svc})
        provider = TracerProvider(resource=resource)

        otlp_endpoint = env_get("OTEL_EXPORTER_OTLP_ENDPOINT", default="")
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
                exporter_kwargs: dict[str, Any] = {"endpoint": otlp_endpoint}
                insecure_env = env_get("OTEL_EXPORTER_OTLP_INSECURE", default="")
                if insecure_env:
                    exporter_kwargs["insecure"] = insecure_env.lower() in {"1", "true", "yes"}
                elif otlp_endpoint.startswith("http://"):
                    exporter_kwargs["insecure"] = True
                timeout_ms = env_get("OTEL_EXPORTER_OTLP_TIMEOUT_MS", default="")
                if timeout_ms:
                    exporter_kwargs["timeout"] = max(float(timeout_ms) / 1000.0, 0.1)
                headers = _parse_otel_headers(env_get("OTEL_EXPORTER_OTLP_HEADERS", default=""))
                if headers:
                    exporter_kwargs["headers"] = headers
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(**exporter_kwargs))
                )
                print(f"[otel] OTLP exporter → {otlp_endpoint} (service={svc})")
            except ImportError:
                print("[otel] OTLP exporter not available — falling back to console")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            print(f"[otel] console exporter active (service={svc})")

        trace.set_tracer_provider(provider)
        _tracer_holder["v"] = trace.get_tracer(svc)
        _tracer_holder["provider"] = provider
        _tracer_holder["configured"] = True

    except ImportError:
        print("[otel] opentelemetry-sdk not installed — tracing disabled")


def get_tracer():
    """Return the tracer (or a no-op object if OTEL not configured)."""
    return _tracer_holder["v"]


def current_trace_id() -> str:
    try:
        from opentelemetry import trace

        span_obj = trace.get_current_span()
        ctx = span_obj.get_span_context() if span_obj else None
        if ctx and getattr(ctx, "is_valid", False):
            return f"{ctx.trace_id:032x}"
    except Exception:
        pass
    return ""


def force_flush_otel(timeout_millis: int = 5000) -> bool:
    """Flush pending spans so verification scripts can assert export explicitly."""
    provider = _tracer_holder.get("provider")
    if provider is None:
        return False
    try:
        return bool(provider.force_flush(timeout_millis=timeout_millis))
    except Exception:
        return False


@contextmanager
def span(name: str, **attributes):
    """
    Context manager that creates a span if OTEL is active, otherwise no-op.

    Usage:
        with span("db.query", sql=query_text):
            result = run_query()
    """
    tracer = _tracer_holder["v"]
    if tracer is not None:
        with tracer.start_as_current_span(name) as s:
            for k, v in attributes.items():
                try:
                    s.set_attribute(k, str(v))
                except Exception:
                    pass
            yield s
    else:
        with nullcontext() as s:
            yield s


def instrument_fastapi(app) -> None:
    """Auto-instrument a FastAPI app (adds spans for every HTTP request)."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        print("[otel] FastAPI instrumented")
    except ImportError:
        pass


def instrument_psycopg() -> None:
    """Auto-instrument psycopg DB calls."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
        PsycopgInstrumentor().instrument()
        print("[otel] psycopg instrumented")
    except ImportError:
        pass


def instrument_redis() -> None:
    """Auto-instrument Redis calls."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
        print("[otel] redis instrumented")
    except ImportError:
        pass


def instrument_httpx() -> None:
    """Auto-instrument httpx outbound calls."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        print("[otel] httpx instrumented")
    except ImportError:
        pass


def bootstrap_worker_otel(service_name: str) -> None:
    """
    Lightweight bootstrap for long-running workers.

    This keeps tracing setup consistent across the control plane without
    forcing each worker wrapper to repeat the same instrumentation boilerplate.
    """
    configure_otel(service_name)
    instrument_psycopg()
    instrument_redis()
    instrument_httpx()
