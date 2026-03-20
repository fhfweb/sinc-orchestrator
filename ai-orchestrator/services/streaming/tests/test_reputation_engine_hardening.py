from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_reputation_engine_update_postgres_uses_tenant_scoped_rls():
    from services.reputation_engine import ReputationEngine

    engine = ReputationEngine("tenant-a")
    seen: dict[str, object] = {}

    class _FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        async def commit(self):
            return None

    @asynccontextmanager
    async def _fake_async_db(*, tenant_id=None, bypass_rls=False):
        seen["tenant_id"] = tenant_id
        seen["bypass_rls"] = bypass_rls
        yield _FakeConn()

    with patch("services.reputation_engine.async_db", _fake_async_db), patch(
        "services.reputation_engine.get_table_columns_cached",
        AsyncMock(return_value=[]),
    ):
        await engine._update_postgres(
            "tenant-a",
            "backend",
            "ai-engineer",
            True,
            1200,
        )

    assert seen == {"tenant_id": "tenant-a", "bypass_rls": False}


@pytest.mark.asyncio
async def test_reputation_engine_rejects_missing_tenant_for_local_worker():
    from services.reputation_engine import ReputationEngine

    engine = ReputationEngine("local")
    with patch.object(engine, "_update_redis", AsyncMock()) as update_redis, patch.object(
        engine,
        "_update_postgres",
        AsyncMock(),
    ) as update_postgres:
        await engine._process_audit_event(
            {
                "task_type": "backend",
                "agent_name": "ai engineer",
                "status": "done",
                "duration_ms": 1200,
            }
        )

    update_redis.assert_not_awaited()
    update_postgres.assert_not_awaited()


@pytest.mark.asyncio
async def test_reputation_engine_stop_cancels_registered_background_tasks():
    from services.reputation_engine import ReputationEngine

    engine = ReputationEngine("tenant-a")
    cancelled = False

    async def _never_finishes():
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    engine._spawn_background_task(_never_finishes(), name="reputation.test")
    await asyncio.sleep(0)
    await engine.stop()

    assert cancelled is True
    assert engine._background_tasks.has_live_tasks(engine._task_owner) is False
