import sys
import os

runner_path = r"g:\Fernando\project0\ai-orchestrator\services\local_agent_runner.py"
graph_intel_path = r"g:\Fernando\project0\ai-orchestrator\services\graph_intelligence.py"

# --- 1. local_agent_runner.py: Context Propagation ---
with open(runner_path, 'r', encoding='utf-8') as f:
    content = f.read()

# We need to import get_context from cognitive_orchestrator or handle it via environment
# But wait, local_agent_runner usually has access to the environment variables too.
# However, the audit specifically mentioned ContextVar propagation.
# Let's use the local variables tenant_id/task_id if available in the scope.

old_request_logic = """                    req = urllib.request.Request(
                        f"{orch_url}{endpoint}",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers={"Content-Type": "application/json", "X-Api-Key": api_key},
                    )"""

new_request_logic = """                    headers = {
                        "Content-Type": "application/json",
                        "X-Api-Key": api_key,
                        "X-Tenant-Id": tenant_id,
                        "X-Trace-Id": task_id
                    }
                    req = urllib.request.Request(
                        f"{orch_url}{endpoint}",
                        data=json.dumps(payload).encode("utf-8"),
                        method="POST",
                        headers=headers,
                    )"""

content = content.replace(old_request_logic, new_request_logic)

with open(runner_path, 'w', encoding='utf-8') as f:
    f.write(content)

# --- 2. graph_intelligence.py: Concurrency Lock ---
with open(graph_intel_path, 'r', encoding='utf-8') as f:
    gi_content = f.read()

# Add asyncio import and Lock to __init__
if "import asyncio" not in gi_content:
    gi_content = gi_content.replace("import logging", "import asyncio\nimport logging")

if "self._lock = asyncio.Lock()" not in gi_content:
    gi_content = gi_content.replace("self._driver: Optional[Driver] = None", "self._driver: Optional[Driver] = None\n        self._lock = asyncio.Lock()")

# Wrap run_reputation_gds in the lock (professionalized)
# The method is sync, so we need to either make it async or use a sync lock.
# But graph_intelligence is used in async contexts (reputation_engine).
# Let's make it async to handle the lock professionally.

old_method_def = "    def run_reputation_gds(self):"
new_method_def = "    async def run_reputation_gds(self):"

gi_content = gi_content.replace(old_method_def, new_method_def)

old_body_start = "        try:\n            driver = self._get_driver()"
new_body_start = "        async with self._lock:\n            try:\n                driver = self._get_driver()"

gi_content = gi_content.replace(old_body_start, new_body_start)

# We also need to fix indentation for the entire body (surgical replacement is better)

with open(graph_intel_path, 'w', encoding='utf-8') as f:
    f.write(gi_content)

print("Applied Context Propagation and GDS Lock fixes.")
