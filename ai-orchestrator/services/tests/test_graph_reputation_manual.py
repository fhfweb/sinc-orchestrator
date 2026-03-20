import asyncio
import logging
from services.graph_intelligence import GraphIntelligenceService

logging.basicConfig(level=logging.INFO)

async def test_reputation_sync():
    gi = GraphIntelligenceService()
    print(f"Testing Neo4j connection to {gi._uri} as {gi._user}...")
    
    # 1. Sync sample outcomes
    print("Syncing sample data to Neo4j...")
    gi.sync_task_outcome(
        tenant_id="test_tenant",
        agent_name="agent_alpha",
        task_id="task_1",
        task_type="fix_bug",
        status="done",
        duration_ms=5000,
        files_affected=["services/agent_worker.py", "services/http_client.py"]
    )
    
    gi.sync_task_outcome(
        tenant_id="test_tenant",
        agent_name="agent_beta",
        task_id="task_2",
        task_type="create_route",
        status="done",
        duration_ms=3000,
        files_affected=["services/streaming/routes/tasks.py"]
    )
    
    # 2. Run GDS (Note: This will fail if GDS is not installed in the target Neo4j)
    print("Running GDS algorithms...")
    await gi.run_reputation_gds()
    
    # 3. Check metrics
    print("Fetching metrics for agent_alpha...")
    metrics = gi.get_agent_metrics("agent_alpha", "test_tenant")
    print(f"Metrics: {metrics}")
    
    gi.close()

if __name__ == "__main__":
    asyncio.run(test_reputation_sync())
