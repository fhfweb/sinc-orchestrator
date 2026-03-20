from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from services.streaming.core import runtime_plane
from services.streaming.routes import dashboard_api
from services.streaming.routes import health as health_routes

_FIXTURE_LOGS_DIR = dashboard_api.Path("g:/Fernando/project0/ai-orchestrator/services/streaming/tests/fixtures/diagnostic_logs")


def _build_app():
    app = FastAPI()
    app.include_router(dashboard_api.router)
    app.dependency_overrides[dashboard_api.get_tenant_id] = lambda: "local"
    return app


def test_diagnostics_health_returns_canonical_components(monkeypatch):
    async def fake_readiness(_tenant_id: str = "local"):
        return {
            "status": "degraded",
            "health": "degraded",
            "quality": "degraded",
            "counts": {
                "pending": 3,
                "active_agents": 2,
                "open_incidents": 1,
            },
            "cognitive": {
                "status": "limited",
                "score": 0.74,
                "critical_missing": [],
                "optional_missing": ["graph_reasoning"],
                "summary": "graph reasoning unavailable",
            },
            "ts": "2026-03-19T12:00:00+00:00",
        }

    async def fake_health_deep(_response: Response):
        return {
            "status": "degraded",
            "quality": "degraded",
            "layers": {
                "l0_redis": "ok",
                "l1_postgres": "ok",
                "l2_neo4j": "not_configured",
                "l3_qdrant": "timeout after 0.5s",
                "l4_llm": "configured",
                "event_bus": "ok",
                "ollama": "configured",
            },
            "cognitive": {
                "quality_status": "limited",
            },
            "ts": "2026-03-19T12:00:01+00:00",
        }

    monkeypatch.setattr(runtime_plane, "compute_readiness_snapshot", fake_readiness)
    monkeypatch.setattr(health_routes, "health_deep", fake_health_deep)

    client = TestClient(_build_app())
    response = client.get("/api/v5/dashboard/diagnostics/health")

    assert response.status_code == 200
    body = response.json()
    assert body["health"] == "degraded"
    assert body["components"]["runtime"]["status"] == "warn"
    assert body["components"]["cognitive"]["status"] == "warn"
    assert body["components"]["postgres"]["status"] == "up"
    assert body["components"]["neo4j"]["status"] == "warn"
    assert body["components"]["qdrant"]["status"] == "err"
    assert "open_incidents=1" in body["issues"]


def test_diagnostics_logs_supports_multi_component_queries(monkeypatch):
    monkeypatch.setenv("LOGS_DIR", str(_FIXTURE_LOGS_DIR))

    client = TestClient(_build_app())
    response = client.get(
        "/api/v5/dashboard/diagnostics/logs?components=worker,orch&pattern=TASK-9&since_hours=72&limit=50"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["components_requested"] == ["worker", "orch"]
    assert body["totals"]["ERROR"] == 2
    assert body["totals"]["WARN"] == 1
    assert len(body["components"]) == 2
    assert body["components"][0]["returned"] >= 1
    assert body["patterns"]
    assert body["anomalies"]
    assert body["recommendations"]


def test_diagnostics_logs_prefers_stream_projection(monkeypatch):
    class _FakeRedis:
        async def xrevrange(self, *_args, **_kwargs):
            return [
                (
                    "3-0",
                    {
                        "data": dashboard_api.json.dumps(
                            {
                                "component": "orch",
                                "line": "[2026-03-19T10:03:00Z] ERROR queue stalled TASK-9",
                                "level": "ERROR",
                                "fingerprint": "ERROR queue stalled TASK-<id>",
                                "ts": "2026-03-19T10:03:00+00:00",
                                "source_path": "orchestrator.log",
                            }
                        )
                    },
                ),
                (
                    "2-0",
                    {
                        "data": dashboard_api.json.dumps(
                            {
                                "component": "worker",
                                "line": "[2026-03-19T10:02:00Z] ERROR queue stalled TASK-9",
                                "level": "ERROR",
                                "fingerprint": "ERROR queue stalled TASK-<id>",
                                "ts": "2026-03-19T10:02:00+00:00",
                                "source_path": "agent_worker.log",
                            }
                        )
                    },
                ),
                (
                    "1-0",
                    {
                        "data": dashboard_api.json.dumps(
                            {
                                "component": "worker",
                                "line": "[2026-03-19T10:01:00Z] WARN retry TASK-9",
                                "level": "WARN",
                                "fingerprint": "WARN retry TASK-<id>",
                                "ts": "2026-03-19T10:01:00+00:00",
                                "source_path": "agent_worker.log",
                            }
                        )
                    },
                ),
            ]

    async def fake_project(*_args, **_kwargs):
        return {"ok": True, "projected": 3}

    monkeypatch.setattr(dashboard_api, "get_async_redis", lambda: _FakeRedis())
    monkeypatch.setattr(dashboard_api, "_project_diagnostic_logs_once", fake_project)

    client = TestClient(_build_app())
    response = client.get(
        "/api/v5/dashboard/diagnostics/logs?components=worker,orch&pattern=TASK-9&since_hours=72&limit=50"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["components_requested"] == ["worker", "orch"]
    assert body["totals"]["ERROR"] == 2
    assert body["totals"]["WARN"] == 1
    assert body["patterns"]
    assert body["anomalies"]
