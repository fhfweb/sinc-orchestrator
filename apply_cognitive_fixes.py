import sys
import os

file_path = r"g:\Fernando\project0\ai-orchestrator\services\cognitive_orchestrator.py"

if not os.path.exists(file_path):
    print(f"Error: {file_path} not found")
    sys.exit(1)

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Multi-Tenancy: Remove default="local" from CognitiveTask.tenant_id
# From: tenant_id: str = "local"
# To:   tenant_id: str
content = content.replace('tenant_id: str = "local"', 'tenant_id: str', 1) # Only first occurrence (the model)

# 2. Semantic Batching: Implement Type-Based Batching in process_batch
old_batch_logic = """        # 2. Parallel Processing (Future: Semantic Batching clustering)
        # For now, we use a simple Gather
        batch_results = await asyncio.gather(*[self.process(t) for t in admitted], return_exceptions=True)"""

new_batch_logic = """        # 2. Semantic Batching (Type-Based Grouping)
        groups: Dict[str, List[CognitiveTask]] = {}
        for t in admitted:
            groups.setdefault(t.task_type, []).append(t)
        
        all_tasks_to_run = []
        for ttype, tlist in groups.items():
            log.debug("batch_group_execution type=%s size=%d", ttype, len(tlist))
            all_tasks_to_run.extend(tlist)

        batch_results = await asyncio.gather(*[self.process(t) for t in all_tasks_to_run], return_exceptions=True)"""

content = content.replace(old_batch_logic, new_batch_logic)

# 3. Transactional Retry: process_llm_retry_queue
# We need to be careful with the loop logic. 
# Original:
#     for payload in payloads:
#         raw = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
#         await redis.zrem(key, payload)
#         data = json.loads(raw)
#         ...
#         await enqueue_llm_retry(task_id, tenant_id, attempt=attempt + 1)
#         requeued.append({"task_id": task_id, "attempt": attempt + 1})

# New: Move zrem to AFTER success.

old_retry_loop = """    for payload in payloads:
        raw = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
        await redis.zrem(key, payload)
        data = json.loads(raw)
        attempt = int(data.get("attempt", 0))
        task_id = str(data.get("task_id") or "")
        if attempt >= _LLM_RETRY_MAX_ATTEMPTS:
            async with async_db(tenant_id=tenant_id, bypass_rls=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE tasks SET status = 'dead-letter' WHERE tenant_id = %s AND task_id = %s",
                        (tenant_id, task_id),
                    )
            continue
        await enqueue_llm_retry(task_id, tenant_id, attempt=attempt + 1)
        requeued.append({"task_id": task_id, "attempt": attempt + 1})"""

new_retry_loop = """    for payload in payloads:
        raw = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
        data = json.loads(raw)
        attempt = int(data.get("attempt", 0))
        task_id = str(data.get("task_id") or "")
        
        try:
            if attempt >= _LLM_RETRY_MAX_ATTEMPTS:
                async with async_db(tenant_id=tenant_id, bypass_rls=True) as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE tasks SET status = 'dead-letter' WHERE tenant_id = %s AND task_id = %s",
                            (tenant_id, task_id),
                        )
                await redis.zrem(key, payload) # Transactional: remove after DB update
                continue
            
            # Transactional: Re-enqueue before removing from current queue
            await enqueue_llm_retry(task_id, tenant_id, attempt=attempt + 1)
            await redis.zrem(key, payload)
            requeued.append({"task_id": task_id, "attempt": attempt + 1})
        except Exception as e:
            log.error("retry_requeue_failed task_id=%s error=%s", task_id, e)"""

content = content.replace(old_retry_loop, new_retry_loop)

# 4. Final safety: remove default tenant from set_context too? 
# Line 51: def set_context(tenant_id: str, trace_id: str = "none", project_id: str = "") -> OrchestratorContext:
# It's already fine (no default).

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f"Successfully applied fixes to {file_path}")
