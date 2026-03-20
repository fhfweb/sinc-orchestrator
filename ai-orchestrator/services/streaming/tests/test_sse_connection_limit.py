"""
tests/test_sse_connection_limit.py
====================================
Tests for SSE connection tracking (Phase 1.1).
Verifies SADD/SCARD/SREM logic and per-plan limit enforcement
without requiring a live Redis instance.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Unit tests for sse.py functions ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_connection_register_calls_sadd_and_expire():
    """connection_register must SADD the conn_id and set an EXPIRE on the key."""
    mock_pipe = AsyncMock()
    mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_pipe.__aexit__  = AsyncMock(return_value=False)

    mock_redis = MagicMock()
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    with patch("streaming.core.sse.get_async_redis", return_value=mock_redis):
        from services.streaming.core.sse import connection_register
        await connection_register("tenant-1", "conn-abc")

    mock_pipe.sadd.assert_awaited_once_with("sse_conns:tenant-1", "conn-abc")
    mock_pipe.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_connection_unregister_calls_srem():
    """connection_unregister must SREM the conn_id from the key."""
    mock_redis = AsyncMock()

    with patch("streaming.core.sse.get_async_redis", return_value=mock_redis):
        from services.streaming.core.sse import connection_unregister
        await connection_unregister("tenant-1", "conn-abc")

    mock_redis.srem.assert_awaited_once_with("sse_conns:tenant-1", "conn-abc")


@pytest.mark.asyncio
async def test_connection_count_returns_scard():
    """connection_count must return the SCARD value from Redis."""
    mock_redis = AsyncMock()
    mock_redis.scard = AsyncMock(return_value=3)

    with patch("streaming.core.sse.get_async_redis", return_value=mock_redis):
        from services.streaming.core.sse import connection_count
        count = await connection_count("tenant-1")

    assert count == 3


@pytest.mark.asyncio
async def test_connection_count_returns_zero_when_redis_unavailable():
    """If Redis is None, connection_count must return 0 (graceful degradation)."""
    with patch("streaming.core.sse.get_async_redis", return_value=None):
        from services.streaming.core.sse import connection_count
        assert await connection_count("tenant-x") == 0


# ── Integration-style: plan limit enforcement ─────────────────────────────────

@pytest.mark.asyncio
async def test_free_plan_blocks_third_connection():
    """
    A free-plan tenant has a limit of 2 SSE connections.
    The third attempt must be rejected with HTTP 429.
    """
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from services.streaming.routes.events import router as events_router

    app = FastAPI()
    app.include_router(events_router)

    # Simulate 2 existing connections (SCARD = 2)
    mock_redis = AsyncMock()
    mock_redis.scard = AsyncMock(return_value=2)

    tenant_stub = {"id": "t1", "plan": "free"}

    with patch("streaming.core.sse.get_async_redis", return_value=mock_redis), \
         patch("streaming.routes.events.get_tenant_id", return_value="t1"), \
         patch("streaming.routes.events.get_tenant",    return_value=tenant_stub):

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/events")

    assert response.status_code == 429


@pytest.mark.asyncio
async def test_pro_plan_allows_up_to_limit():
    """A pro-plan tenant (limit 20) should accept connection when count < 20."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from services.streaming.routes.events import router as events_router

    app = FastAPI()
    app.include_router(events_router)

    mock_redis = AsyncMock()
    mock_redis.scard   = AsyncMock(return_value=5)   # well under the pro limit of 20
    mock_pipe = AsyncMock()
    mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_pipe.__aexit__  = AsyncMock(return_value=False)
    mock_redis.pipeline = MagicMock(return_value=mock_pipe)

    tenant_stub = {"id": "t2", "plan": "pro"}

    async def _noop_gen():
        return
        yield  # make it a generator

    with patch("streaming.core.sse.get_async_redis", return_value=mock_redis), \
         patch("streaming.routes.events.get_tenant_id", return_value="t2"), \
         patch("streaming.routes.events.get_tenant",    return_value=tenant_stub), \
         patch("streaming.routes.events.get_event_iterator", return_value=_noop_gen()):

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/events")

    # 200 OK (SSE stream opened) — not 429
    assert response.status_code != 429
