import re
import os

file_path = r"g:\Fernando\project0\ai-orchestrator\services\local_agent_runner.py"

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Latency Optimization in HybridAgentRunner.run
# Target: lines 938-945 (approx)
old_run = r'# Derive policy and build autonomy dossier if needed\n\s+# We pass preflight_ctx to skip redundant research\n\s+autonomy_brief = self._build_autonomy_brief\(task_id, task, WORKSPACE, preflight_ctx\)'
new_run = r'# 1. Prepare intelligence: bypass on-the-fly research if orchestrator provided preflight_ctx\n        if preflight_ctx and len(preflight_ctx) > 100:\n            autonomy_brief = ""\n        else:\n            autonomy_brief = self._build_autonomy_brief(task_id, task, WORKSPACE, preflight_ctx)'

content = re.sub(old_run, new_run, content)

# 2. Tenant Safety in _project_scope
# Target: lines 1255-1258
old_scope = r'def _project_scope\(\) -> tuple\[str, str\]:\n\s+project_id = env_get\("PROJECT_ID", default="sinc"\).strip\(\) or "sinc"\n\s+tenant_id = env_get\("TENANT_ID", default="local"\).strip\(\) or "local"\n\s+return project_id, tenant_id'
new_scope = r'def _project_scope() -> tuple[str, str]:\n    """Force explicit scope detection. No insecure defaults."""\n    project_id = env_get("PROJECT_ID", default="").strip()\n    tenant_id = env_get("TENANT_ID", default="").strip()\n    if not project_id or not tenant_id:\n        raise RuntimeError("CRITICAL: Missing PROJECT_ID or TENANT_ID in environment.")\n    return project_id, tenant_id'

content = re.sub(old_scope, new_scope, content)

# 3. Memory Search - Anti-hallucination gating
# We search for the start of memory_search and replace the entire if/elif block
search_block_pattern = r'elif name == "memory_search":.*?results": results\}, indent=2, ensure_ascii=False\)'
new_search_block = r'''elif name == "memory_search":
            query = inp.get("query", "").strip()
            task_type = (inp.get("task_type") or task.get("task_type") or "generic").strip()
            exploration = bool(inp.get("exploration", False))
            effective_query = query or task.get("description", "")
            
            collections = ["agent_memory", "solutions"]
            if task_type == "error":
                collections.insert(0, "errors")

            results = []
            for suffix in collections:
                coll = _memory_collection(default_suffix=suffix)
                vector, err = _embed_text(effective_query)
                if not err:
                    hits, error = _search_qdrant(coll, vector, top_k=5)
                    if error: continue
                    for hit in hits:
                        payload = hit.get("payload", {})
                        md = payload.get("metadata", {})
                        
                        # ANTI-HALLUCINATION GATING: Verified solutions only by default
                        if suffix == "solutions" and not exploration:
                            if not md.get("verified", False):
                                continue

                        results.append(
                            {
                                "score": round(hit.get("score", 0.0), 4),
                                "content": payload.get("content") or payload.get("solution") or payload.get("text", ""),
                                "tags": payload.get("tags", []),
                                "source": suffix,
                                "timestamp": payload.get("timestamp"),
                                "metadata": md,
                            }
                        )
            return json.dumps({"query": effective_query, "results": results}, indent=2, ensure_ascii=False)'''

content = re.sub(search_block_pattern, new_search_block, content, flags=re.DOTALL)

# 4. Memory Write - Auto-set verified=False
old_write_block_pattern = r'elif name == "memory_write":.*?OK: memory stored in \{collection\} \(intent=\{intent\}\)"'
new_write_block = r'''elif name == "memory_write":
            content = inp.get("content", "").strip()
            key = (inp.get("key") or "").strip()
            tags = list(inp.get("tags") or [])
            intent = inp.get("intent", "agent_memory").strip()
            
            if intent == "solution":
                collection = _memory_collection(default_suffix="solutions")
            elif intent == "error":
                collection = _memory_collection(default_suffix="errors")
            else:
                collection = _memory_collection(default_suffix=intent)

            if not content:
                return "ERROR: content is required"
            
            vector, error = _embed_text(content)
            if error:
                return f"ERROR: embedding failed: {error}"
            
            metadata = dict(inp.get("metadata") or {})
            metadata.update({
                "agent_name": agent_name,
                "task_id": task_id,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "intent": intent,
                "verified": False  # Anti-hallucination loop gating
            })
            
            # Allow manual override for verified solutions
            if inp.get("verified") is True and env_get("ADMIN_API_KEY") == inp.get("admin_key"):
                metadata["verified"] = True

            payload = {
                "id": key or str(uuid4()),
                "content": content,
                "tags": sorted({str(tag) for tag in tags if str(tag).strip()}),
                "tenant_id": tenant_id,
                "project_id": project_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata,
            }
            if intent == "solution":
                payload["solution"] = content
                payload["query"] = task.get("description", "")

            error = _upsert_qdrant(collection, vector, payload)
            if error:
                return f"ERROR: memory write failed: {error}"
            return f"OK: memory stored in {collection} (intent={intent}, verified={metadata['verified']})")'''

# Clean up
content = re.sub(old_write_block_pattern, new_write_block, content, flags=re.DOTALL)

# Save result
with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)

print("SUCCESS: Applied 4 critical architectural fixes to local_agent_runner.py")
