import asyncio
import sys
import os
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.cognitive_orchestrator import get_orchestrator, set_context, get_context
from services.mcts_planner import get_planner

async def verify():
    print("--- Professionalization Verification ---")
    orch = get_orchestrator()
    
    # 1. Verify Context Propagation
    print("\n1. Testing Context Propagation...")
    set_context(tenant_id="professional-v2", trace_id="trace-777")
    ctx = get_context()
    print(f"   Context Set: tenant={ctx.tenant_id}, trace={ctx.trace_id}")
    
    # Check if MCTS Planner observes the same tenant
    from services.mcts_planner import _get_active_tenant
    active_tenant = _get_active_tenant()
    print(f"   MCTS Observed Tenant: {active_tenant}")
    assert active_tenant == "professional-v2", "Tenant propagation failed!"
    print("   ✓ Context Propagation OK")

    # 2. Verify Component Registry & Health
    print("\n2. Testing Component Registry & Health...")
    await orch.initialize()
    health = await orch.registry.check_health()
    print(f"   Registry Health: {health.keys()}")
    assert "planner" in health, "Planner missing from registry!"
    assert "memory" in health, "Memory missing from registry!"
    print("   ✓ Registry & Health OK")

    # 4. Verify LLM Solver Registry
    print("\n4. Testing LLM Solver Registry...")
    solver = orch.registry.get("llm_solver")
    print(f"   Solver found in registry: {solver is not None}")
    assert solver is not None, "LLMSolver missing from registry!"
    print("   ✓ LLM Solver OK")

    # 5. Verify Cognitive Graph Stability
    print("\n5. Testing Cognitive Graph Stability...")
    from services.cognitive_graph import get_cognitive_graph, CognitiveState
    graph = get_cognitive_graph()
    print(f"   Graph compiled successfully. Nodes: {graph.nodes.keys()}")
    
    # Simple state validation
    test_state = CognitiveState(
        task={}, description="test", task_type="generic", project_id="p1", tenant_id="t1"
    )
    print(f"   Pydantic State Validated: id={test_state.task}")
    print("   ✓ Cognitive Graph OK")

    print("\n--- Verification Complete: ALL SYSTEMS NOMINAL ---")

if __name__ == "__main__":
    asyncio.run(verify())
