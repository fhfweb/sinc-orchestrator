from fastapi import Response
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_health_deep_degrades_on_critical_cognitive_gaps():
    from services.streaming.routes import health as health_routes

    async def _ok():
        return "ok"

    with patch.object(health_routes, "_probe_redis", new=_ok), patch.object(
        health_routes, "_probe_postgres", new=_ok
    ), patch.object(health_routes, "_probe_neo4j", new=_ok), patch.object(
        health_routes, "_probe_qdrant", new=_ok
    ), patch.object(health_routes, "_probe_llm", new=_ok), patch.object(
        health_routes, "_probe_event_bus", new=_ok
    ), patch(
        "services.cognitive_orchestrator.get_cognitive_capability_snapshot_async",
        new=AsyncMock(return_value={
            "initialized": True,
            "init_attempted": True,
            "quality_status": "limited",
            "score": 0.5,
            "critical_missing": ["planner"],
            "optional_missing": [],
            "components": {},
            "summary": "critical gaps: planner",
        }),
    ):
        response = Response()
        payload = await health_routes.health_deep(response)

    assert response.status_code == 503
    assert payload["status"] == "degraded"
    assert payload["quality"] == "degraded"
    assert payload["cognitive"]["quality_status"] == "limited"
    assert payload["layers"]["cognitive_orchestrator"] == "limited"
