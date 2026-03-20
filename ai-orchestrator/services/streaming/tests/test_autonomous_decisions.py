import pytest

from services.autonomous_decisions import apply_autonomous_decisions
from services.intelligence_router import IntelligenceDepth, IntelligenceStrategy


class _FakeRedis:
    async def zrevrange(self, key, start, end, withscores=False):
        assert withscores is True
        return [
            ("architect", 0.92),
            ("ai engineer", 0.80),
            ("qa agent", 0.60),
        ]


@pytest.mark.asyncio
async def test_apply_autonomous_decisions_switches_agent_when_top3_delta_is_high(monkeypatch):
    monkeypatch.setattr(
        "services.streaming.core.redis_.get_async_redis",
        lambda: _FakeRedis(),
    )

    strategy = IntelligenceStrategy(
        depth=IntelligenceDepth.STANDARD,
        use_neo4j=False,
        use_qdrant=True,
        use_prediction=True,
        use_proactive_injection=False,
        max_retrieval_ms=400,
        confidence_threshold=0.75,
        auto_decompose_if_low_confidence=True,
        require_impact_assessment=False,
        reasoning="test",
    )

    updates, actions = await apply_autonomous_decisions(
        state={
            "tenant_id": "local",
            "task_type": "backend",
            "task": {"assigned_agent": "qa agent"},
        },
        context={
            "recommended_agent": "architect",
            "success_prediction": {"success_rate": 0.91},
        },
        strategy=strategy,
    )

    assert updates["assigned_agent"] == "architect"
    assert any(action.action_type == "switch_agent" for action in actions)


@pytest.mark.asyncio
async def test_apply_autonomous_decisions_keeps_agent_when_delta_is_small(monkeypatch):
    class _TightRedis:
        async def zrevrange(self, key, start, end, withscores=False):
            return [
                ("architect", 0.91),
                ("ai engineer", 0.89),
                ("qa agent", 0.88),
            ]

    monkeypatch.setattr(
        "services.streaming.core.redis_.get_async_redis",
        lambda: _TightRedis(),
    )

    strategy = IntelligenceStrategy(
        depth=IntelligenceDepth.STANDARD,
        use_neo4j=False,
        use_qdrant=True,
        use_prediction=True,
        use_proactive_injection=False,
        max_retrieval_ms=400,
        confidence_threshold=0.75,
        auto_decompose_if_low_confidence=True,
        require_impact_assessment=False,
        reasoning="test",
    )

    updates, actions = await apply_autonomous_decisions(
        state={
            "tenant_id": "local",
            "task_type": "backend",
            "task": {"assigned_agent": "ai engineer"},
        },
        context={
            "recommended_agent": "architect",
            "success_prediction": {"success_rate": 0.91},
        },
        strategy=strategy,
    )

    assert "assigned_agent" not in updates
    assert not any(action.action_type == "switch_agent" for action in actions)
