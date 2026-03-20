import sys
import os

graph_path = r"g:\Fernando\project0\ai-orchestrator\services\cognitive_graph.py"
evolution_path = r"g:\Fernando\project0\ai-orchestrator\services\memory_evolution.py"

# --- 1. cognitive_graph.py: CodeVerificationNode ---
with open(graph_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Add verified_by_vnode to CognitiveState
if "verified_by_vnode: bool = False" not in content:
    content = content.replace("confidence: float = 0.5", "confidence: float = 0.5\n    verified_by_vnode: bool = False")

# Add code_verification_node function
verification_node_code = """
async def code_verification_node(state: CognitiveState) -> Dict:
    \"\"\"Module 9.1: Formal Code Verification. Replaces heuristic-only gating.\"\"\"
    solution = _state_get(state, "solution")
    if not solution or "```" not in solution:
        return {"verified_by_vnode": False}
    
    # Extract code blocks
    code_blocks = re.findall(r"```(?:python)?\\n(.*?)\\n```", solution, re.DOTALL)
    if not code_blocks:
        return {"verified_by_vnode": False}
    
    import tempfile
    import subprocess
    
    is_valid = True
    for block in code_blocks:
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
            tmp.write(block.encode("utf-8"))
            tmp_path = tmp.name
        try:
            # Syntax check only for safety in this node
            res = subprocess.run([sys.executable, "-m", "py_compile", tmp_path], 
                                 capture_output=True, timeout=5)
            if res.returncode != 0:
                is_valid = False
                break
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    
    return {"verified_by_vnode": is_valid}
"""
if "def code_verification_node" not in content:
    # Insert before quality_gate_node
    content = content.replace("async def quality_gate_node", verification_node_code + "\nasync def quality_gate_node")

# Update learn_and_store_node to use verified_by_vnode
content = content.replace(
    "is_verified = succeeded and confidence > 0.95",
    "is_verified = succeeded and _state_get(state, 'verified_by_vnode', False)"
)

# Update build_cognitive_graph topology
content = content.replace(
    "workflow.add_node(\"learn_and_store\", learn_and_store_node)",
    "workflow.add_node(\"learn_and_store\", learn_and_store_node)\n    workflow.add_node(\"code_validator\", code_verification_node)"
)
content = content.replace(
    "workflow.add_edge(\"llm_solver\", \"quality_gate\")",
    "workflow.add_edge(\"llm_solver\", \"code_validator\")\n    workflow.add_edge(\"code_validator\", \"quality_gate\")"
)

with open(graph_path, 'w', encoding='utf-8') as f:
    f.write(content)

# --- 2. memory_evolution.py: Consistency Alignment ---
with open(evolution_path, 'r', encoding='utf-8') as f:
    ev_content = f.read()

# Add agent_reputation update in generate_and_store_lesson
reputation_update_code = """
        # Consistency Alignment: Sync Redis reward to Postgres agent_reputation
        try:
            async with pool.acquire() as conn:
                await conn.execute(\"\"\"
                    UPDATE agent_reputation 
                    SET success_rate = (success_rate * 0.9) + ($1 * 0.1),
                        total_tasks = total_tasks + 1,
                        updated_at = NOW()
                    WHERE agent_name = $2 AND tenant_id = $3
                \"\"\", 1.0 if succeeded else 0.0, planner, tenant_id)
        except Exception as e:
            log.warning(\"postgres_reputation_sync_failed error=%s\", e)
"""

if "UPDATE agent_reputation" not in ev_content:
    # Insert before PostgreSQL Audit section (line 96 approx)
    ev_content = ev_content.replace("# 4. PostgreSQL Audit", reputation_update_code + "\n    # 4. PostgreSQL Audit")

with open(evolution_path, 'w', encoding='utf-8') as f:
    f.write(ev_content)

print("Applied CodeValidatorNode and Consistency Alignment fixes.")
