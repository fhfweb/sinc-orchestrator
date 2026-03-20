from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.streaming.routes import entropy


def _build_app():
    app = FastAPI()
    app.include_router(entropy.router)
    app.dependency_overrides[entropy.get_tenant_id] = lambda: "local"
    return app


def test_entropy_risk_context_returns_profile_and_matches(monkeypatch):
    rows = [
        {
            "project_id": "sinc",
            "tenant_id": "local",
            "file_path": "services/foo.py",
            "entropy_score": 0.82,
            "label": "critical",
            "complexity": 14,
            "coupling": 9,
            "max_fn_lines": 50,
            "scan_at": "2026-03-19T10:00:00+00:00",
        },
        {
            "project_id": "sinc",
            "tenant_id": "local",
            "file_path": "services/bar.py",
            "entropy_score": 0.44,
            "label": "healthy",
            "complexity": 3,
            "coupling": 1,
            "max_fn_lines": 10,
            "scan_at": "2026-03-19T10:00:00+00:00",
        },
    ]

    class _FakeCursor:
        async def execute(self, _sql, _params):
            return None

        async def fetchall(self):
            return rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    @asynccontextmanager
    async def fake_async_db(**_kwargs):
        yield _FakeConn()

    monkeypatch.setattr(entropy, "async_db", fake_async_db)

    client = TestClient(_build_app())
    response = client.get("/entropy/risk-context?project_id=sinc&files=services/foo.py,services/missing.py")

    assert response.status_code == 200
    body = response.json()
    assert body["profile"] == "guarded"
    assert body["count"] == 1
    assert body["files"][0]["file_path"] == "services/foo.py"
    assert body["recommendations"]
