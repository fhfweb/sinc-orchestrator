import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from services.streaming.routes import cognitive


def _build_app():
    app = FastAPI()
    app.include_router(cognitive.router)
    app.dependency_overrides[cognitive.get_tenant_id] = lambda: "local"
    return app


def test_cognitive_batch_enqueues_job(monkeypatch):
    calls = {"create": []}

    async def fake_create(batch_job_id, tenant_id, tasks, **kwargs):
        calls["create"].append((batch_job_id, tenant_id, tasks, kwargs))
        return 3

    async def fake_count(_tenant_id, **_kwargs):
        return 0

    async def fake_sum(_tenant_id, **_kwargs):
        return 0

    monkeypatch.setattr(cognitive, "_create_cognitive_batch_job", fake_create)
    monkeypatch.setattr(cognitive, "_count_cognitive_batch_jobs_for_tenant", fake_count)
    monkeypatch.setattr(cognitive, "_sum_cognitive_batch_estimated_tokens_for_tenant", fake_sum)

    client = TestClient(_build_app())
    response = client.post(
        "/cognitive/batch",
        json={
            "tasks": [
                {"id": "T-1", "task_type": "fix_bug", "title": "Fix bug", "description": "repair path"},
                {"id": "T-2", "task_type": "review", "title": "Review", "description": "review patch"},
            ]
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["batch_job_id"].startswith("cbatch-")
    assert body["total"] == 2
    assert body["status"] == "pending"
    assert body["queue_position"] == 3
    assert body["estimated_tokens"] > 0
    assert calls["create"]


def test_cognitive_batch_rejects_when_tenant_queue_is_full(monkeypatch):
    async def fake_count(_tenant_id, **_kwargs):
        return cognitive.COGNITIVE_BATCH_MAX_PENDING_PER_TENANT

    monkeypatch.setattr(cognitive, "_count_cognitive_batch_jobs_for_tenant", fake_count)

    client = TestClient(_build_app())
    response = client.post(
        "/cognitive/batch",
        json={"tasks": [{"id": "T-1", "task_type": "fix_bug", "title": "Fix bug", "description": "repair path"}]},
    )

    assert response.status_code == 429
    assert "queue is full" in response.json()["detail"]


def test_cognitive_batch_rejects_when_token_budget_is_too_large(monkeypatch):
    async def fake_count(_tenant_id, **_kwargs):
        return 0

    async def fake_sum(_tenant_id, **_kwargs):
        return 0

    monkeypatch.setattr(cognitive, "_count_cognitive_batch_jobs_for_tenant", fake_count)
    monkeypatch.setattr(cognitive, "_sum_cognitive_batch_estimated_tokens_for_tenant", fake_sum)
    monkeypatch.setattr(cognitive, "COGNITIVE_BATCH_MAX_ESTIMATED_TOKENS_PER_JOB", 50)

    client = TestClient(_build_app())
    response = client.post(
        "/cognitive/batch",
        json={
            "tasks": [
                {
                    "id": "T-1",
                    "task_type": "fix_bug",
                    "title": "Fix bug",
                    "description": "x" * 1200,
                }
            ]
        },
    )

    assert response.status_code == 429
    assert "token quota exceeded" in response.json()["detail"]


def test_cognitive_batch_status_reads_persisted_job(monkeypatch):
    async def fake_fetch(batch_job_id, tenant_id):
        return {
            "job": {"batch_job_id": batch_job_id, "tenant_id": tenant_id, "status": "completed"},
            "items": [{"task_id": "T-1", "status": "completed"}],
        }

    monkeypatch.setattr(cognitive, "_fetch_cognitive_batch_job", fake_fetch)

    client = TestClient(_build_app())
    response = client.get("/cognitive/batch/cbatch-123")

    assert response.status_code == 200
    body = response.json()
    assert body["job"]["status"] == "completed"
    assert body["items"][0]["task_id"] == "T-1"


@pytest.mark.asyncio
async def test_cognitive_batch_queue_worker_processes_claimed_job(monkeypatch):
    calls = {"persist": [], "complete": []}

    class _FakeOrchestrator:
        async def process_batch(self, tasks):
            return [
                SimpleNamespace(
                    task_id=task["id"],
                    solution=f"solution:{task['id']}",
                    planner="mcts",
                    cache_level="l2",
                    llm_used=index == 0,
                    tokens_used=12 + index,
                    latency_ms=25 + index,
                    error=None,
                )
                for index, task in enumerate(tasks)
            ]

    sequence = iter(
        [
            {"batch_job_id": "cbatch-1", "tenant_id": "local"},
            None,
        ]
    )

    async def fake_claim(_worker_id):
        return next(sequence)

    async def fake_fetch_tasks(_batch_job_id, _tenant_id):
        return [
            {"id": "T-1", "task_type": "fix_bug", "title": "Fix bug"},
            {"id": "T-2", "task_type": "review", "title": "Review"},
        ]

    async def fake_persist(batch_job_id, tenant_id, tasks, results):
        calls["persist"].append((batch_job_id, tenant_id, tasks, results))

    async def fake_complete(batch_job_id, tenant_id, **kwargs):
        calls["complete"].append((batch_job_id, tenant_id, kwargs))

    monkeypatch.setattr(cognitive, "_get_orchestrator", lambda: _FakeOrchestrator())
    monkeypatch.setattr(cognitive, "_claim_next_cognitive_batch_job", fake_claim)
    monkeypatch.setattr(cognitive, "_fetch_cognitive_batch_tasks", fake_fetch_tasks)
    monkeypatch.setattr(cognitive, "_persist_cognitive_batch_items", fake_persist)
    monkeypatch.setattr(cognitive, "_complete_cognitive_batch_job", fake_complete)
    monkeypatch.setattr(cognitive, "COGNITIVE_BATCH_CONCURRENCY", 1)

    started = await cognitive.run_cognitive_batch_queue_once()
    assert started == 1
    if cognitive._cognitive_batch_running:
        await asyncio.gather(*list(cognitive._cognitive_batch_running))

    assert calls["persist"]
    assert calls["complete"][0][2]["status"] == "completed"


def test_cognitive_memory_reactivation_route(monkeypatch):
    async def fake_hints(**kwargs):
        assert kwargs["project_id"] == "sinc"
        assert kwargs["task_type"] == "fix_bug"
        assert kwargs["file_path"] == "services/foo.py"
        return [{"hint_kind": "file_path", "summary": "Reuse prior fix", "strength": 0.9}]

    async def fake_schema():
        return None

    monkeypatch.setattr(cognitive, "fetch_reactivation_hints", fake_hints)
    monkeypatch.setattr(cognitive, "ensure_memory_compaction_schema", fake_schema)

    client = TestClient(_build_app())
    response = client.get("/cognitive/memory/reactivation?project_id=sinc&task_type=fix_bug&file_path=services/foo.py")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["summary"] == "Reuse prior fix"
