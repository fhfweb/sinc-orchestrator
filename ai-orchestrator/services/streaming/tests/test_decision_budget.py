from types import SimpleNamespace

import pytest

from services.decision_budget import execute_decisions_with_budget


class _FakeRedis:
    async def zrevrange(self, key, start, end, withscores=False):
        assert withscores is True
        return [
            ("architect", 0.92),
            ("ai engineer", 0.80),
            ("qa agent", 0.60),
        ]


@pytest.mark.asyncio
async def test_execute_decisions_with_budget_switches_agent_via_shared_policy(monkeypatch):
    monkeypatch.setattr(
        "services.streaming.core.redis_.get_async_redis",
        lambda: _FakeRedis(),
    )
    monkeypatch.setattr(
        "services.streaming.core.db.get_pool",
        lambda: None,
    )

    task = SimpleNamespace(
        id="TASK-1",
        status="pending",
        assigned_agent="qa agent",
        task_type="backend",
        autonomous_actions=[],
    )
    confidence = SimpleNamespace(
        require_human_gate=False,
        recommended_strategy="execute_direct",
        recommended_agent="architect",
        confidence_level="high",
    )

    updated_task, budget = await execute_decisions_with_budget(task, confidence, "local")

    assert updated_task.assigned_agent == "architect"
    assert "switch_agent" in budget.applied
    assert any(action.startswith("switch_agent:") for action in updated_task.autonomous_actions)
