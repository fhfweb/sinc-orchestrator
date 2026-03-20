import asyncio

import httpx

from services.http_client import create_resilient_client


def test_resilient_client_injects_trace_and_service_headers():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with create_resilient_client(
            service_name="unit-test",
            transport=transport,
            timeout=1.0,
        ) as client:
            response = await client.get("http://example.test/ping")
            assert response.status_code == 200

    asyncio.run(run())

    assert captured["headers"]["x-orchestrator-service"] == "unit-test"
    assert captured["headers"]["user-agent"] == "sinc-orchestrator/unit-test"
    assert captured["headers"]["x-trace-id"]


def test_resilient_client_preserves_custom_headers():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, request=request)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with create_resilient_client(
            service_name="dashboard-api",
            headers={"X-Api-Key": "dev", "X-Custom": "present"},
            transport=transport,
            timeout=1.0,
        ) as client:
            await client.get("http://example.test/health")

    asyncio.run(run())

    assert captured["headers"]["x-api-key"] == "dev"
    assert captured["headers"]["x-custom"] == "present"
    assert captured["headers"]["x-orchestrator-service"] == "dashboard-api"
