import importlib

import pytest


def test_reputation_worker_resolves_tenant_from_env(monkeypatch):
    mod = importlib.import_module("services.reputation_worker")

    monkeypatch.delenv("TENANT_ID", raising=False)
    monkeypatch.delenv("ORCHESTRATOR_TENANT_ID", raising=False)
    monkeypatch.delenv("ORCH_TENANT_ID", raising=False)
    assert mod._resolve_worker_tenant_id() == "local"

    monkeypatch.setenv("TENANT_ID", "tenant-from-legacy")
    assert mod._resolve_worker_tenant_id() == "tenant-from-legacy"

    monkeypatch.setenv("ORCHESTRATOR_TENANT_ID", "tenant-from-orchestrator")
    assert mod._resolve_worker_tenant_id() == "tenant-from-orchestrator"

    monkeypatch.setenv("ORCH_TENANT_ID", "tenant-from-orch")
    assert mod._resolve_worker_tenant_id() == "tenant-from-orch"


@pytest.mark.asyncio
async def test_async_get_agent_reputation_score_prefers_tenant_scoped_key(monkeypatch):
    from services.streaming.core import redis_ as redis_mod

    class FakeRedis:
        async def hget(self, key, field):
            data = {
                ("agent:rep:tenant-a:agent-x", "score"): "0.91",
                ("agent:rep:agent-x", "score"): "0.42",
            }
            return data.get((key, field))

    monkeypatch.setattr(redis_mod, "get_async_redis", lambda: FakeRedis())

    scoped = await redis_mod.async_get_agent_reputation_score("agent-x", "tenant-a")
    legacy = await redis_mod.async_get_agent_reputation_score("agent-x", "tenant-b")

    assert scoped == pytest.approx(0.91)
    assert legacy == pytest.approx(0.42)
