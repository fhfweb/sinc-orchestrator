import sys
import os

orchestrator_path = r"g:\Fernando\project0\ai-orchestrator\services\cognitive_orchestrator.py"
graph_path = r"g:\Fernando\project0\ai-orchestrator\services\cognitive_graph.py"

# --- 1. cognitive_orchestrator.py: MissingTenantError ---
with open(orchestrator_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Add Exception class
if "class MissingTenantError" not in content:
    exception_code = """
class MissingTenantError(ValueError):
    \"\"\"Raised when a tenant_id is missing or null in the cognitive pipeline.\"\"\"
    pass
"""
    # Insert before CognitiveTask
    content = content.replace("class CognitiveTask(BaseModel):", exception_code + "\nclass CognitiveTask(BaseModel):")

# Use it in set_context
old_set_context = """def set_context(tenant_id: str, trace_id: str = "none", project_id: str = "") -> OrchestratorContext:
    ctx = OrchestratorContext(tenant_id=tenant_id, trace_id=trace_id, project_id=project_id)
    _context.set(ctx)
    return ctx"""

new_set_context = """def set_context(tenant_id: str, trace_id: str = "none", project_id: str = "") -> OrchestratorContext:
    if not tenant_id or tenant_id == "local": # 'local' is no longer allowed as a masked default
         raise MissingTenantError("Explicit tenant_id is required for Pillar III operations.")
    ctx = OrchestratorContext(tenant_id=tenant_id, trace_id=trace_id, project_id=project_id)
    _context.set(ctx)
    return ctx"""

content = content.replace(old_set_context, new_set_context)

with open(orchestrator_path, 'w', encoding='utf-8') as f:
    f.write(content)

# --- 2. cognitive_graph.py: Autonomous Learning Gate ---
with open(graph_path, 'r', encoding='utf-8') as f:
    graph_content = f.read()

# Modify learn_and_store_node to check confidence
old_learn_node = """async def learn_and_store_node(state: CognitiveState) -> Dict:
    \"\"\"Evolutionary Memory Layer.\"\"\"
    solution = _state_get(state, "solution")
    error = _state_get(state, "error")
    succeeded = bool(solution and not error and "[error" not in (solution or "").lower())
    
    try:
        from services.memory_evolution import generate_and_store_lesson
        payload = state.model_dump() if hasattr(state, "model_dump") else dict(state)
        await generate_and_store_lesson(payload, solution or "", succeeded, error)"""

new_learn_node = """async def learn_and_store_node(state: CognitiveState) -> Dict:
    \"\"\"Evolutionary Memory Layer with Autonomous Verification Gate.\"\"\"
    solution = _state_get(state, "solution")
    error = _state_get(state, "error")
    confidence = float(_state_get(state, "confidence_score") or 0.0)
    succeeded = bool(solution and not error and "[error" not in (solution or "").lower())
    
    # Autonomous Gating: Only mark as verified if confidence is exceptionally high
    is_verified = succeeded and confidence > 0.95

    try:
        from services.memory_evolution import generate_and_store_lesson
        payload = state.model_dump() if hasattr(state, "model_dump") else dict(state)
        # We'll pass verified=is_verified to ensure the learning subagent logic holds
        await generate_and_store_lesson(payload, solution or "", succeeded, error, verified=is_verified)"""

graph_content = graph_content.replace(old_learn_node, new_learn_node)

with open(graph_path, 'w', encoding='utf-8') as f:
    f.write(graph_content)

print("Applied MissingTenantError and Learning Gate fixes.")
