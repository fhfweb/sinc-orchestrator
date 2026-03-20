from __future__ import annotations

import asyncio
import pytest

from services.graph_intelligence import GraphIntelligenceService


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def single(self):
        return self._row


class _FakeSession:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, **kwargs):
        if "RETURN gds.version()" in query:
            return _FakeResult({"v": "2.8"})
        if "CALL gds.graph.exists" in query:
            return _FakeResult({"exists": self.state["exists"]})
        if "CALL gds.graph.drop" in query:
            self.state["drop_calls"] += 1
            self.state["exists"] = False
            return _FakeResult({})
        if "CALL gds.graph.project" in query:
            self.state["project_calls"] += 1
            self.state["exists"] = True
            return _FakeResult({})
        if "CALL gds.pageRank.write" in query:
            self.state["pagerank_calls"] += 1
            return _FakeResult({})
        if "CALL gds.degree.write" in query:
            self.state["degree_calls"] += 1
            return _FakeResult({})
        raise AssertionError(query)


class _FakeDriver:
    def __init__(self, state):
        self.state = state

    def session(self):
        return _FakeSession(self.state)


class _LeaseRedis:
    def __init__(self):
        self.set_calls = []
        self.eval_calls = []

    async def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        return True

    async def mget(self, *_args, **_kwargs):
        return [None, None]

    async def eval(self, script, _numkeys, *args):
        self.eval_calls.append((script, args))
        if "EXPIRE" in script:
            return 1
        return 1


@pytest.mark.asyncio
async def test_run_reputation_gds_reuses_projection_when_fresh(monkeypatch):
    state = {
        "exists": False,
        "project_calls": 0,
        "drop_calls": 0,
        "pagerank_calls": 0,
        "degree_calls": 0,
    }
    service = GraphIntelligenceService(uri="bolt://unit", user="neo4j", password="neo4j")
    service._gds_min_run_interval_s = 0
    service._gds_projection_refresh_interval_s = 999999
    monkeypatch.setattr(service, "_get_driver", lambda: _FakeDriver(state))

    first = await service.run_reputation_gds(iterations=3)
    second = await service.run_reputation_gds(iterations=3)

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert state["project_calls"] == 1
    assert state["drop_calls"] == 0
    assert state["pagerank_calls"] == 2
    assert state["degree_calls"] == 2


@pytest.mark.asyncio
async def test_run_reputation_gds_skips_when_distributed_lease_is_held(monkeypatch):
    state = {
        "exists": False,
        "project_calls": 0,
        "drop_calls": 0,
        "pagerank_calls": 0,
        "degree_calls": 0,
    }

    class _LeaseHeldRedis:
        async def set(self, *_args, **_kwargs):
            return None

        async def mget(self, *_args, **_kwargs):
            return [None, None]

    service = GraphIntelligenceService(uri="bolt://unit", user="neo4j", password="neo4j")
    monkeypatch.setattr(service, "_get_driver", lambda: _FakeDriver(state))
    monkeypatch.setattr("services.graph_intelligence.get_async_redis", lambda: _LeaseHeldRedis())

    result = await service.run_reputation_gds(iterations=3)

    assert result["status"] == "skipped"
    assert result["reason"] == "lease_held"
    assert state["project_calls"] == 0


@pytest.mark.asyncio
async def test_renew_distributed_lease_extends_owned_token(monkeypatch):
    redis = _LeaseRedis()
    service = GraphIntelligenceService(uri="bolt://unit", user="neo4j", password="neo4j")
    monkeypatch.setattr("services.graph_intelligence.get_async_redis", lambda: redis)

    renewed = await service._renew_distributed_lease("token-1")

    assert renewed is True
    assert redis.eval_calls
    assert "EXPIRE" in redis.eval_calls[0][0]


@pytest.mark.asyncio
async def test_run_reputation_gds_starts_and_cancels_lease_heartbeat(monkeypatch):
    state = {
        "exists": False,
        "project_calls": 0,
        "drop_calls": 0,
        "pagerank_calls": 0,
        "degree_calls": 0,
    }
    redis = _LeaseRedis()
    service = GraphIntelligenceService(uri="bolt://unit", user="neo4j", password="neo4j")
    service._gds_min_run_interval_s = 0
    service._gds_projection_refresh_interval_s = 999999
    monkeypatch.setattr(service, "_get_driver", lambda: _FakeDriver(state))
    monkeypatch.setattr("services.graph_intelligence.get_async_redis", lambda: redis)

    heartbeat = {"started": 0, "cancelled": 0}

    async def _fake_heartbeat(_token: str):
        heartbeat["started"] += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            heartbeat["cancelled"] += 1
            raise

    monkeypatch.setattr(service, "_lease_heartbeat_loop", _fake_heartbeat)

    result = await service.run_reputation_gds(iterations=3)

    assert result["status"] == "ok"
    assert heartbeat == {"started": 1, "cancelled": 1}
