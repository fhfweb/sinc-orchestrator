from __future__ import annotations

import pytest


class _FakeRedis:
    def __init__(self, count: int = 9):
        self.count = count
        self.set_calls: list[tuple[str, int]] = []

    async def incr(self, _key: str) -> int:
        self.count += 1
        return self.count

    async def set(self, key: str, value: int) -> None:
        self.set_calls.append((key, value))


class _FakeRegistry:
    def __init__(self, *, live: bool = False):
        self.live = live
        self.spawn_calls: list[tuple[str, str]] = []

    def has_live_tasks(self, owner: str) -> bool:
        return self.live

    def spawn(self, owner: str, awaitable, *, name: str):
        self.spawn_calls.append((owner, name))
        awaitable.close()
        return None


class _FakeDistiller:
    async def extract_verified_traces(self, _tenant_id: str):
        return None


@pytest.mark.asyncio
async def test_maybe_schedule_distillation_uses_registry_and_resets_counter(monkeypatch):
    from services import memory_evolution

    redis_client = _FakeRedis(count=9)
    registry = _FakeRegistry(live=False)

    monkeypatch.setattr(memory_evolution, "get_background_task_registry", lambda: registry)
    monkeypatch.setattr(memory_evolution, "get_distillation_service", lambda: _FakeDistiller())

    scheduled = await memory_evolution._maybe_schedule_distillation(
        "tenant-a",
        verified=True,
        redis_client=redis_client,
    )

    assert scheduled is True
    assert registry.spawn_calls == [("memory_distillation:tenant-a", "memory.distillation:tenant-a")]
    assert redis_client.set_calls == [("sinc:verified_count:tenant-a", 0)]


@pytest.mark.asyncio
async def test_maybe_schedule_distillation_skips_when_job_is_already_running(monkeypatch):
    from services import memory_evolution

    redis_client = _FakeRedis(count=9)
    registry = _FakeRegistry(live=True)

    monkeypatch.setattr(memory_evolution, "get_background_task_registry", lambda: registry)
    monkeypatch.setattr(memory_evolution, "get_distillation_service", lambda: _FakeDistiller())

    scheduled = await memory_evolution._maybe_schedule_distillation(
        "tenant-a",
        verified=True,
        redis_client=redis_client,
    )

    assert scheduled is False
    assert registry.spawn_calls == []
    assert redis_client.set_calls == []
