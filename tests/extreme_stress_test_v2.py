"""
extreme_stress_test_v2.py
=========================
Aggressive stress test that captures internal status errors.
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
    log.info("starting_reputation_flood count=%d", count)
    tasks = []
    for i in range(count):
        data = {
            "tenant_id": tenant_id, "agent_name": agent_name,
            "task_type": "stress", "completion_status": "success",
            "duration_ms": 100, "task_id": str(uuid4())
        }
        tasks.append(engine._process_audit_event(data))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_errors = []
    for r in results:
        if isinstance(r, Exception): all_errors.append(str(r))
        elif isinstance(r, dict) and r.get("status") == "error": all_errors.append(r.get("error"))
    return all_errors

async def simulate_gds_contention(tenant_id, concurrency=5):
    log.info("starting_gds_contention count=%d", concurrency)
    gi = get_graph_intelligence()
    tasks = [gi.run_reputation_gds(tenant_id, force=True) for _ in range(concurrency)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_errors = []
    for r in results:
        if isinstance(r, Exception): all_errors.append(str(r))
        elif isinstance(r, dict) and r.get("status") == "error": all_errors.append(r.get("error"))
    return all_errors

async def simulate_memory_pressure(tenant_id, count=10):
    log.info("starting_memory_pressure count=%d", count)
    tasks = []
    for i in range(count):
        state = {
            "task": {"id": str(uuid4())}, "tenant_id": tenant_id,
            "project_id": "stress", "description": f"t{i}",
            "task_type": "logic", "start_time": time.time()
        }
        tasks.append(generate_and_store_lesson(state, "sol", True, verified=True))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_errors = []
    import traceback
    for r in results:
        if isinstance(r, Exception):
            err_msg = "".join(traceback.format_exception(type(r), r, r.__traceback__))
            all_errors.append(err_msg)
    return all_errors


async def main():
    tenant_id = "stress_test_v2"
    engine = ReputationEngine(tenant_id=tenant_id)
    start_time = time.perf_counter()
    log.info("--- HELL-MODE V2 STARTING ---")
    
    # Increase concurrency to force failures
    results = await asyncio.gather(
        simulate_reputation_flood(engine, tenant_id, "alpha", 20),
        simulate_gds_contention(tenant_id, 10),
        simulate_memory_pressure(tenant_id, 15),
        return_exceptions=True
    )
    
    duration = time.perf_counter() - start_time
    total_errors = sum(len(r) for r in results if isinstance(r, list))
    log.info("--- TEST COMPLETE in %.2fs. Errors found: %d ---", duration, total_errors)
    
    for i, r in enumerate(results):
        if isinstance(r, list) and r:
             log.error("Component %d errors: %s", i, r[:3]) # Show first 3 errors

if __name__ == "__main__":
    asyncio.run(main())
