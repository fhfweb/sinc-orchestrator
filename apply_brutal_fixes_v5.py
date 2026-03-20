import sys
import os

engine_path = r"g:\Fernando\project0\ai-orchestrator\services\reputation_engine.py"

# --- reputation_engine.py: Single Source of Truth ---
with open(engine_path, 'r', encoding='utf-8') as f:
    engine_content = f.read()

# Modify _process_audit_event to update Postgres first, then Redis (as a cache sync)
old_event_proc = """        await self._update_redis(tenant_id, task_type, agent_name, succeeded, duration_ms)
        await self._update_postgres(tenant_id, agent_name, duration_ms)"""

new_event_proc = """        # Single Source of Truth: Postgres first, then sync to Redis cache
        await self._update_postgres(tenant_id, agent_name, duration_ms)
        await self._update_redis(tenant_id, task_type, agent_name, succeeded, duration_ms)"""

engine_content = engine_content.replace(old_event_proc, new_event_proc)

# I should also update _update_redis to potentially read from postgres?
# But for now, ensuring the order is a good start for "Verdade vs Cache".

with open(engine_path, 'w', encoding='utf-8') as f:
    f.write(engine_content)

print("Applied Single Source of Truth alignment in ReputationEngine.")
