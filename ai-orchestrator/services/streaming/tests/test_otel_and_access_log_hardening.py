import logging
import types
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from services.streaming.core.auth import _is_expected_auth_noise
from services.streaming.routes import system as system_routes
from services.streaming_server_v2 import _ExpectedAuthNoiseFilter


def test_expected_auth_noise_filter_drops_known_dashboard_403():
    filt = _ExpectedAuthNoiseFilter()
    noisy = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", "/api/v5/dashboard/summary", "1.1", 403),
        exc_info=None,
    )
    assert filt.filter(noisy) is False

    real_error = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", "/api/v5/dashboard/task-debugger/T1", "1.1", 500),
        exc_info=None,
    )
    assert filt.filter(real_error) is True


def test_otel_probe_route_returns_probe_id():
    app = FastAPI()

    @app.middleware("http")
    async def inject_trace_id(request: Request, call_next):
        request.state.trace_id = request.headers.get("X-Trace-Id", "")
        return await call_next(request)

    app.include_router(system_routes.router)
    app.dependency_overrides[system_routes.get_tenant_id] = lambda: "local"

    client = TestClient(app)
    response = client.post("/otel/probe", headers={"X-Trace-Id": "trace-test-123"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["probe_id"].startswith("probe-")
    assert body["trace_id"] == "trace-test-123"
    assert "flushed" in body


def test_expected_auth_noise_paths_cover_dashboard_feed_and_config():
    assert _is_expected_auth_noise("/api/v5/dashboard/feed") is True
    assert _is_expected_auth_noise("/api/v5/dashboard/config") is True
    assert _is_expected_auth_noise("/system/infra") is True
    assert _is_expected_auth_noise("/tasks/claim") is True
    assert _is_expected_auth_noise("/tasks") is True
    assert _is_expected_auth_noise("/agents/complete") is False


def test_otel_collector_config_has_file_exporter():
    root = Path(__file__).resolve().parents[3]
    config = (root / "docker" / "otel-collector-config.yaml").read_text(encoding="utf-8")
    assert "file/traces" in config
    assert "/var/lib/otel/traces.jsonl" in config


def test_verify_otel_export_prefers_file_artifact(monkeypatch, capsys):
    from scripts import verify_otel_export

    class _Completed:
        def __init__(self, stdout="", stderr=""):
            self.stdout = stdout
            self.stderr = stderr

    fixed_uuid = types.SimpleNamespace(hex="1234567890abcdef1234567890abcdef")

    def fake_run(args, **kwargs):
        if args[:3] == ["docker", "exec", "sinc-otel-collector"]:
            return _Completed(stdout='{"name":"otel.explicit_probe","probe_id":"probe-1234567890ab"}')
        raise AssertionError(f"unexpected subprocess args: {args}")

    monkeypatch.setattr(verify_otel_export.subprocess, "run", fake_run)
    monkeypatch.setattr(verify_otel_export.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_otel_export.uuid, "uuid4", lambda: fixed_uuid)
    monkeypatch.setattr(verify_otel_export.sys, "argv", ["verify_otel_export.py"])
    monkeypatch.setenv("OTEL_ENABLED", "false")

    code = verify_otel_export.main()
    out = capsys.readouterr().out
    assert code == 0
    assert "probe_source=collector-file" in out
    assert "status=ok" in out


def test_verify_otel_export_falls_back_to_runtime_probe(monkeypatch, capsys):
    from scripts import verify_otel_export

    class _Completed:
        def __init__(self, stdout="", stderr=""):
            self.stdout = stdout
            self.stderr = stderr

    class _RuntimeResponse:
        def read(self, *_args, **_kwargs):
            return b'{"ok":true,"probe_id":"probe-1234567890ab"}'

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    fixed_uuid = types.SimpleNamespace(hex="1234567890abcdef1234567890abcdef")

    monkeypatch.setattr(verify_otel_export, "_emit_local_probe", lambda *_args, **_kwargs: (False, "local-sdk-unavailable"))
    monkeypatch.setattr(verify_otel_export.urllib.request, "urlopen", lambda *_args, **_kwargs: _RuntimeResponse())
    monkeypatch.setattr(
        verify_otel_export.subprocess,
        "run",
        lambda args, **kwargs: _Completed(stdout='{"name":"otel.explicit_probe","probe_id":"probe-1234567890ab"}'),
    )
    monkeypatch.setattr(verify_otel_export.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(verify_otel_export.uuid, "uuid4", lambda: fixed_uuid)
    monkeypatch.setattr(verify_otel_export.sys, "argv", ["verify_otel_export.py"])

    code = verify_otel_export.main()
    out = capsys.readouterr().out
    assert code == 0
    assert "emit_source=runtime-http" in out
    assert "status=ok" in out
