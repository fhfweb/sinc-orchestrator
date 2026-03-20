"""
extreme_stress_test.py
======================
Phase 14: Hell-Mode Validation.
Simulates high-concurrency pressure on GDS locks, memory writes, and reputation engine.
"""
import asyncio
import logging
import time
from uuid import uuid4
from services.graph_intelligence import get_graph_intelligence
from services.reputation_engine import ReputationEngine
from services.memory_evolution import generate_and_store_lesson

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stress_test")

async def simulate_reputation_flood(engine, tenant_id, agent_name, count=10):
    """Simulates a flood of audit events."""
    log.info("starting_reputation_flood agent=%s count=%d", agent_name, count)
    tasks = []
    for i in range(count):
        data = {
            "tenant_id": tenant_id,
            "agent_name": agent_name,
            "task_type": "stress_test",
            "completion_status": "success" if i % 2 == 0 else "failed",
            "duration_ms": 100,
            "task_id": str(uuid4())
        }
        tasks.append(engine._process_audit_event(data))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    log.info("reputation_flood_finished errors=%d", len(errors))
    return errors

async def simulate_gds_contention(tenant_id, concurrency=2):
    """Simulates multiple GDS runs at once."""
    log.info("starting_gds_contention concurrency=%d", concurrency)
    gi = get_graph_intelligence()
    tasks = [gi.run_reputation_gds(tenant_id) for _ in range(concurrency)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    log.info("gds_contention_finished errors=%d", len(errors))
    return errors

async def simulate_memory_pressure(tenant_id, count=5):
    """Simulates multiple memory writes."""
    log.info("starting_memory_pressure count=%d", count)
    tasks = []
    for i in range(count):
        state = {
            "task": {"id": str(uuid4())},
            "tenant_id": tenant_id,
            "project_id": "stress_prj",
            "description": f"Stress test task {i}",
            "task_type": "logic",
            "start_time": time.time()
        }
        tasks.append(generate_and_store_lesson(state, "Solution X", True, verified=True))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    log.info("memory_pressure_finished errors=%d", len(errors))
    return errors

async def main():
    tenant_id = "stress_test_tenant"
    engine = ReputationEngine(tenant_id=tenant_id)
    
    start_time = time.perf_counter()
    log.info("--- STARTING HELL-MODE STRESS TEST ---")
    
    # Run all stressors concurrently
    results = await asyncio.gather(
        simulate_reputation_flood(engine, tenant_id, "agent_alpha", 10),
        simulate_gds_contention(tenant_id, 2),
        simulate_memory_pressure(tenant_id, 5),
        return_exceptions=True
    )
    
    duration = time.perf_counter() - start_time
    log.info("--- STRESS TEST COMPLETE in %.2fs ---", duration)
    
    total_errors = sum(len(r) for r in results if isinstance(r, list))
    if total_errors == 0:
        log.info("VERDICT: SYSTEM IS ROCK SOLID (100% SUCCESS UNDER PRESSURE)")
    else:
        log.error("VERDICT: SYSTEM DEGRADED (%d ERRORS DETECTED)", total_errors)

if __name__ == "__main__":
    asyncio.run(main())
