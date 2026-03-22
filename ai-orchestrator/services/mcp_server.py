import os
import json
import uuid
import httpx
from mcp.server.fastmcp import FastMCP
from services.ast_analyzer import ASTAnalyzer
from services.impact_analyzer import ImpactAnalyzer
from services.flow_mapper import FlowMapper
from services.streaming.core.config import env_get
from services.http_client import create_resilient_client

# Initialize FastMCP server
mcp = FastMCP("SINC Cognitive Server")

# Helper to get Neo4j driver (using shared config if possible)
def get_analyzer():
    return ASTAnalyzer()

async def _orchestrator_request(method: str, path: str, body: dict = None, tenant_id: str = "local") -> dict:
    base_url = env_get("ORCHESTRATOR_URL", default="http://localhost:8000").rstrip("/")
    api_key = env_get("ORCHESTRATOR_API_KEY", default="")

    headers = {
        "X-Tenant-Id": tenant_id,
        "X-Trace-Id": f"mcp-{uuid.uuid4().hex[:8]}",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with create_resilient_client(service_name="mcp-server") as client:
        try:
            response = await client.request(method, f"{base_url}{path}", json=body, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}


# ── Knowledge Graph ────────────────────────────────────────────────────────────

@mcp.tool()
async def query_graph(query: str, project_id: str = "default", tenant_id: str = "local") -> str:
    """
    Search the code knowledge graph for symbols, files, or relationships.
    Best for: 'Where is X defined?', 'Who calls Y?', 'Find classes extending Z'.
    """
    with get_analyzer() as analyzer:
        driver = analyzer._get_driver()
        if not driver:
            return "Neo4j driver not available."

        with driver.session() as session:
            # Simple keyword search fallback if not valid Cypher
            if "MATCH" not in query.upper():
                result = session.run("""
                    MATCH (n {project_id: $pid, tenant_id: $tid})
                    WHERE n.name CONTAINS $q OR n.path CONTAINS $q
                    RETURN n.name as name, n.file as file, labels(n) as type
                    LIMIT 10
                """, q=query, pid=project_id, tid=tenant_id)
            else:
                result = session.run(query, pid=project_id, tid=tenant_id)

            records = [dict(r) for r in result]
            if not records:
                return "No results found."
            return json.dumps(records, indent=2)

@mcp.tool()
async def impact_analysis(symbol_name: str, project_id: str = "default", tenant_id: str = "local") -> str:
    """
    Calculate the blast radius of changing a specific code symbol.
    Identify callers and dependencies affected by a potential change.
    """
    with get_analyzer() as analyzer:
        driver = analyzer._get_driver()
        if not driver:
            return "Neo4j driver not available."

        impact_svc = ImpactAnalyzer(driver)
        result = impact_svc.analyze_impact(symbol_name, project_id, tenant_id)

        if not result["impact_map"]:
            return f"No impact detected for {symbol_name}."

        output = [f"Impact Analysis for '{symbol_name}':"]

        # Red Team Risk Heuristic
        risk_score = len(result["impact_map"]) * 2
        risk_level = "CRITICAL" if risk_score > 15 else "MEDIUM" if risk_score > 5 else "LOW"
        output.append(f"  [!] Red Team Risk Score: {risk_score} ({risk_level})")

        for imp in result["impact_map"]:
            output.append(f"  - [{imp['risk']}] {imp['type']}: {imp['name']} ({imp['file']}) - Depth: {imp['depth']}")

        return "\n".join(output)

@mcp.tool()
async def get_memory_graph_summary(tenant_id: str = "local", limit: int = 30) -> str:
    """
    Get a high-level summary of the L2 Neo4j knowledge graph:
    node counts by type, most-connected symbols, recent file changes.
    Best used at the start of a coding session for orientation.
    """
    with get_analyzer() as analyzer:
        driver = analyzer._get_driver()
        if not driver:
            return "Neo4j driver not available."
        with driver.session() as s:
            counts = s.run(
                "MATCH (n) WHERE n.tenant_id = $tid RETURN labels(n)[0] as type, count(n) as cnt ORDER BY cnt DESC",
                tid=tenant_id,
            )
            hubs = s.run(
                """MATCH (n)-[r]->(m) WHERE n.tenant_id = $tid
                   RETURN n.name as name, n.file as file, count(r) as degree
                   ORDER BY degree DESC LIMIT $lim""",
                tid=tenant_id, lim=limit,
            )
            type_summary = {r["type"]: r["cnt"] for r in counts if r["type"]}
            hub_list = [{"name": r["name"], "file": r["file"], "degree": r["degree"]} for r in hubs]
            return json.dumps({"node_counts": type_summary, "top_hubs": hub_list}, indent=2)


# ── Semantic / Vector Memory ───────────────────────────────────────────────────

@mcp.tool()
async def search_past_solutions(query: str, project_id: str = "sinc", tenant_id: str = "local", top_k: int = 5) -> str:
    """
    Search the SINC L3 semantic vector memory (Qdrant) for past solutions, bugs, and architectures
    previously resolved by the orchestrator. Critical for discovering prior art.
    """
    try:
        from services.context_retriever import ContextRetriever
        retriever = ContextRetriever(top_k=top_k)
        result = retriever.retrieve(query=query, project_id=project_id, tenant_id=tenant_id, top_k=top_k)

        cache_hit = retriever.check_semantic_cache(query=query, project_id=project_id, tenant_id=tenant_id, threshold=0.80)

        output = [f"L3 Memory Search Results for '{query}':"]
        if cache_hit:
            output.append(f"\n[!] HIGH CONFIDENCE SOLUTION MATCH (Score: {cache_hit['score']:.2f})")
            output.append(f"Past Decision/Solution:\n{cache_hit['answer']}")

        chunks = result.get("chunks", [])
        if not chunks and not cache_hit:
            return "No past solutions or chunks found in L3 memory."

        if chunks:
            output.append("\nRelated Code/Context Chunks:")
            for c in chunks:
                snippet = c['text'][:300].replace('\n', ' ')
                output.append(f" - {c['file']} (Score: {c.get('hybrid_score') or c['score']})\n   {snippet}...")

        return "\n".join(output)
    except Exception as e:
        return f"Error connecting to L3 Memory: {e}"

@mcp.tool()
async def memory_write(
    content: str,
    key: str = "",
    tags: list[str] | None = None,
    collection: str = "sinc_memory",
    tenant_id: str = "local",
) -> str:
    """
    Persist a lesson, decision, or reusable code pattern into Qdrant vector memory (L1/L3).
    Use after solving a novel problem so future tasks can benefit from this solution.

    Args:
        content: The text to store (solution, lesson, architectural decision, etc.)
        key: Optional stable identifier for future retrieval
        tags: Optional list of tags for filtering (e.g. ["bug-fix", "auth", "migration"])
        collection: Qdrant collection name (default: sinc_memory)
        tenant_id: Tenant scope
    """
    try:
        from services.semantic_backend import embed_text, upsert_point
        point_id = str(uuid.uuid4())
        vector = embed_text(content)
        payload = {
            "content": content,
            "key": key or point_id,
            "tags": tags or [],
            "tenant_id": tenant_id,
            "source": "mcp_write",
        }
        upsert_point(collection=collection, point_id=point_id, vector=vector, payload=payload)
        return f"Memory stored. id={point_id} collection={collection}"
    except Exception as e:
        return f"Memory write failed: {e}"

@mcp.tool()
async def semantic_code_search(query: str, collection: str = "sinc_code", top_k: int = 8, tenant_id: str = "local") -> str:
    """
    Search the ingested codebase using natural-language semantic search (Qdrant).
    Better than grep when you want 'how does X work?' rather than exact text match.

    Args:
        query: Natural language question about the codebase
        collection: Qdrant collection (default: sinc_code)
        top_k: Number of results to return
        tenant_id: Tenant scope
    """
    try:
        from services.semantic_backend import embed_text, search_points
        vector = embed_text(query)
        results = search_points(
            collection=collection,
            vector=vector,
            top_k=top_k,
            filter_payload={"tenant_id": tenant_id},
        )
        if not results:
            return f"No results in collection '{collection}' for: {query}"
        lines = [f"Semantic search results for '{query}' (top {top_k}):"]
        for i, r in enumerate(results, 1):
            p = r.get("payload", {})
            text = p.get("text", p.get("content", ""))[:250].replace("\n", " ")
            lines.append(f"\n[{i}] score={r.get('score', 0):.3f}  file={p.get('file', '?')}")
            lines.append(f"    {text}...")
        return "\n".join(lines)
    except Exception as e:
        return f"Semantic search error: {e}"


# ── Redis L0 Memory ────────────────────────────────────────────────────────────

@mcp.tool()
async def redis_get(key: str) -> str:
    """
    Read a value from the L0 Redis cache.
    Use for hot state: agent locks, session data, recent task results, feature flags.
    """
    try:
        from services.streaming.core import redis_ as r
        val = r.get(key)
        if val is None:
            return f"(nil) — key not found: {key}"
        return str(val)
    except Exception as e:
        return f"Redis error: {e}"

@mcp.tool()
async def redis_set(key: str, value: str, ttl_seconds: int = 0) -> str:
    """
    Write a value into L0 Redis cache.
    Optionally set a TTL (time-to-live). Use for caching intermediate results.
    """
    try:
        from services.streaming.core import redis_ as r
        if ttl_seconds > 0:
            r.setex(key, ttl_seconds, value)
        else:
            r.set(key, value)
        return f"OK — {key} set (ttl={ttl_seconds}s)" if ttl_seconds else f"OK — {key} set"
    except Exception as e:
        return f"Redis error: {e}"

@mcp.tool()
async def redis_keys(pattern: str = "*", max_results: int = 50) -> str:
    """
    List Redis keys matching a pattern (e.g. 'task:*', 'agent:lock:*').
    Returns key names and types.
    """
    try:
        from services.streaming.core import redis_ as r
        keys = r.keys(pattern)[:max_results]
        if not keys:
            return f"No keys matching '{pattern}'"
        result = []
        for k in keys:
            k_str = k.decode() if isinstance(k, bytes) else k
            try:
                t = r.type(k)
                t_str = t.decode() if isinstance(t, bytes) else t
            except Exception:
                t_str = "?"
            result.append(f"{k_str}  [{t_str}]")
        return "\n".join(result)
    except Exception as e:
        return f"Redis error: {e}"


# ── Task Management ────────────────────────────────────────────────────────────

@mcp.tool()
async def create_sinc_task(title: str, description: str, agent: str = "ai engineer", tenant_id: str = "local") -> str:
    """
    Create a new task in the SINC Orchestrator.
    The orchestrator will dispatch this to the appropriate worker.
    """
    payload = {
        "title": title,
        "description": description,
        "agent": agent
    }
    res = await _orchestrator_request("POST", "/api/v1/tasks", body=payload, tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def get_task_status(task_id: str, tenant_id: str = "local") -> str:
    """Check the status and result of a specific SINC task."""
    res = await _orchestrator_request("GET", f"/api/v1/tasks/{task_id}", tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def list_tasks(
    status: str = "",
    agent: str = "",
    limit: int = 20,
    tenant_id: str = "local",
) -> str:
    """
    List SINC Orchestrator tasks with optional filters.

    Args:
        status: Filter by status: pending | running | completed | failed | blocked
        agent: Filter by agent name (e.g. 'backend engineer', 'security auditor')
        limit: Max results (default 20)
        tenant_id: Tenant scope
    """
    params = {"limit": limit}
    if status:
        params["status"] = status
    if agent:
        params["agent"] = agent
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    res = await _orchestrator_request("GET", f"/api/v1/tasks?{qs}", tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def cancel_task(task_id: str, reason: str = "", tenant_id: str = "local") -> str:
    """
    Cancel a running or pending SINC task.
    Use when a task is stuck, superseded, or triggered by mistake.
    """
    res = await _orchestrator_request(
        "POST", f"/api/v1/tasks/{task_id}/cancel",
        body={"reason": reason or "cancelled via MCP"},
        tenant_id=tenant_id,
    )
    return json.dumps(res, indent=2)


# ── Agent Roster & Control ─────────────────────────────────────────────────────

@mcp.tool()
async def list_agents(tenant_id: str = "local") -> str:
    """
    Return the full SINC agent roster: 21 agents across 6 families with their
    current status, preferred backend, and skill domains.
    """
    try:
        from services.agents_config import AGENTS
        return json.dumps(
            [
                {
                    "id": a.id,
                    "name": a.name,
                    "family": a.family,
                    "backend": a.preferred_backend,
                    "skills": a.skill_domains,
                }
                for a in AGENTS
            ],
            indent=2,
        )
    except Exception:
        res = await _orchestrator_request("GET", "/api/v1/agents", tenant_id=tenant_id)
        return json.dumps(res, indent=2)

@mcp.tool()
async def spawn_agent_task(
    agent_id: str,
    title: str,
    description: str,
    priority: int = 2,
    tenant_id: str = "local",
) -> str:
    """
    Create and immediately dispatch a task to a SPECIFIC agent by ID (e.g. 'AG-05').
    Use when you know exactly which specialist is needed.

    Args:
        agent_id: Agent ID from list_agents (AG-01..AG-21)
        title: Short task title
        description: Full task description with context
        priority: 1=critical, 2=normal, 3=low
        tenant_id: Tenant scope
    """
    res = await _orchestrator_request(
        "POST", "/api/v1/tasks",
        body={"title": title, "description": description, "agent_id": agent_id, "priority": priority},
        tenant_id=tenant_id,
    )
    return json.dumps(res, indent=2)


# ── File Operations (Workspace) ────────────────────────────────────────────────

@mcp.tool()
async def read_workspace_file(path: str, workspace: str = "/workspace") -> str:
    """
    Read a file from the agent workspace.
    Path can be relative to workspace root or absolute.
    Returns up to 50KB of content.
    """
    try:
        full = path if os.path.isabs(path) else os.path.join(workspace, path)
        full = os.path.normpath(full)
        if not os.path.exists(full):
            return f"File not found: {full}"
        size = os.path.getsize(full)
        if size > 50_000:
            return f"File too large ({size} bytes). Use bash_in_sandbox to read specific lines."
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"Read error: {e}"

@mcp.tool()
async def write_workspace_file(path: str, content: str, workspace: str = "/workspace") -> str:
    """
    Write (overwrite) a file in the agent workspace.
    Creates parent directories if needed.
    For surgical edits, prefer patch_workspace_file.
    """
    try:
        full = path if os.path.isabs(path) else os.path.join(workspace, path)
        full = os.path.normpath(full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Written {len(content)} bytes to {full}"
    except Exception as e:
        return f"Write error: {e}"

@mcp.tool()
async def patch_workspace_file(path: str, old_str: str, new_str: str, workspace: str = "/workspace") -> str:
    """
    Surgically replace a specific string in a workspace file (first occurrence).
    Safer than write_workspace_file — fails explicitly if old_str is not found.
    Always prefer this over full-file rewrites for targeted edits.
    """
    try:
        full = path if os.path.isabs(path) else os.path.join(workspace, path)
        full = os.path.normpath(full)
        with open(full, "r", encoding="utf-8") as f:
            original = f.read()
        if old_str not in original:
            return f"ERROR: old_str not found in {full}. No changes made."
        patched = original.replace(old_str, new_str, 1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(patched)
        return f"Patched {full} — replaced {len(old_str)} chars with {len(new_str)} chars."
    except Exception as e:
        return f"Patch error: {e}"

@mcp.tool()
async def list_workspace_files(pattern: str = "**/*", workspace: str = "/workspace") -> str:
    """
    List files in the agent workspace matching a glob pattern.
    Examples: '**/*.py', 'app/Services/*.php', 'src/**/*.ts'
    """
    try:
        import glob as _glob
        base = workspace
        full_pattern = os.path.join(base, pattern)
        matches = _glob.glob(full_pattern, recursive=True)
        files = [m.replace(base, "").lstrip("/\\") for m in matches if os.path.isfile(m)]
        if not files:
            return f"No files matching '{pattern}' in {workspace}"
        return "\n".join(sorted(files)[:200])
    except Exception as e:
        return f"List error: {e}"


# ── Sandbox Execution ──────────────────────────────────────────────────────────

@mcp.tool()
async def bash_in_sandbox(
    command: str,
    workspace: str = "/workspace",
    timeout: int = 60,
) -> str:
    """
    Execute a shell command inside the Docker sandbox workspace (non-interactive).
    Use for: running tests, lint, build, git operations, or any shell task.

    Examples:
        bash_in_sandbox("pytest tests/ -x")
        bash_in_sandbox("php artisan test --filter UserTest")
        bash_in_sandbox("git diff HEAD --stat")
        bash_in_sandbox("npm run build 2>&1 | tail -30")
    """
    try:
        from services.sandbox_service import SandboxService
        svc = SandboxService()
        result = await svc.exec_command(command=command, workspace=workspace, timeout=timeout)
        output = result.get("output", "")
        exit_code = result.get("exit_code", -1)
        if exit_code != 0:
            return f"[exit={exit_code}]\n{output}"
        return output or "(no output)"
    except ImportError:
        # fallback: local subprocess (dev mode)
        import asyncio, subprocess as sp
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=sp.PIPE, stderr=sp.STDOUT,
                cwd=workspace if os.path.isdir(workspace) else None,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode("utf-8", errors="replace")[:8000]
        except asyncio.TimeoutError:
            return f"Command timed out after {timeout}s: {command}"
        except Exception as e2:
            return f"Sandbox exec error: {e2}"
    except Exception as e:
        return f"Sandbox error: {e}"


# ── LSP & Code Intelligence ────────────────────────────────────────────────────

@mcp.tool()
async def get_lsp_definition(filepath: str, line: int, character: int) -> str:
    """
    Query the Language Server Protocol (LSP) bridge for the absolute definition of a symbol.
    Requires absolute filepath and 0-indexed line and character positions.
    Helps resolve 'undefined variable' hallucinations definitively.
    """
    try:
        from services.lsp_bridge.client import LSPClient
        client = LSPClient("pyright-langserver", ["--stdio"])
        await client.start()

        root_dir = os.path.dirname(os.path.abspath(filepath))
        abs_path = root_dir.replace('\\', '/')
        if not abs_path.startswith('/'): abs_path = '/' + abs_path
        uri = f"file://{abs_path}"

        await client.initialize(uri)

        file_abs = os.path.abspath(filepath).replace('\\', '/')
        if not file_abs.startswith('/'): file_abs = '/' + file_abs
        file_uri = f"file://{file_abs}"

        with open(filepath, 'r', encoding='utf-8') as f:
            code = f.read()

        await client.did_open(file_uri, code)
        result = await client.get_definition(file_uri, line, character)
        await client.stop()

        if not result:
            return "No definition found via LSP. Symbol might be built-in or unresolvable."
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"LSP Bridge Error: {str(e)}"

@mcp.tool()
async def analyze_code_file(path: str, mode: str = "full", workspace: str = "/workspace") -> str:
    """
    Parse a source file and return structural analysis: functions, imports, classes,
    complexity metrics, and potential issues.

    Args:
        path: File path (relative to workspace or absolute)
        mode: 'functions' | 'dependencies' | 'complexity' | 'full'
        workspace: Workspace root
    """
    try:
        full = path if os.path.isabs(path) else os.path.join(workspace, path)
        with open(full, "r", encoding="utf-8") as f:
            source = f.read()

        ext = os.path.splitext(full)[1].lower()
        result: dict = {"file": path, "mode": mode}

        if ext == ".py":
            import ast as _ast
            tree = _ast.parse(source)
            funcs = [n.name for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)]
            classes = [n.name for n in _ast.walk(tree) if isinstance(n, _ast.ClassDef)]
            imports_list = []
            for n in _ast.walk(tree):
                if isinstance(n, _ast.Import):
                    imports_list += [a.name for a in n.names]
                elif isinstance(n, _ast.ImportFrom):
                    imports_list.append(f"from {n.module}")
            result.update({
                "functions": funcs,
                "classes": classes,
                "imports": imports_list,
                "lines": source.count("\n"),
            })
        else:
            result["lines"] = source.count("\n")
            result["note"] = f"Deep analysis only available for .py files. Extension: {ext}"

        if mode in ("full", "complexity"):
            result["char_count"] = len(source)
            result["avg_line_length"] = round(len(source) / max(source.count("\n"), 1))

        return json.dumps(result, indent=2)
    except FileNotFoundError:
        return f"File not found: {path}"
    except Exception as e:
        return f"Analysis error: {e}"


# ── Observability ──────────────────────────────────────────────────────────────

@mcp.tool()
async def get_system_metrics(tenant_id: str = "local") -> str:
    """
    Get current orchestrator system metrics: agent queue depths, LLM usage,
    task throughput, memory utilization, and service health.
    """
    res = await _orchestrator_request("GET", "/api/v5/dashboard/metrics", tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def get_agent_logs(agent_id: str = "", task_id: str = "", limit: int = 50, tenant_id: str = "local") -> str:
    """
    Retrieve recent log entries for a specific agent or task.
    Useful for debugging why a task failed or understanding agent behavior.

    Args:
        agent_id: Filter by agent ID (e.g. 'AG-05') — optional
        task_id: Filter by task ID — optional
        limit: Number of log lines to return
        tenant_id: Tenant scope
    """
    params = f"limit={limit}"
    if agent_id:
        params += f"&agent_id={agent_id}"
    if task_id:
        params += f"&task_id={task_id}"
    res = await _orchestrator_request("GET", f"/api/v1/logs?{params}", tenant_id=tenant_id)
    return json.dumps(res, indent=2)


# ── Feature Flags ──────────────────────────────────────────────────────────────

@mcp.tool()
async def get_feature_flags(tenant_id: str = "local") -> str:
    """List all feature flags and their current state."""
    res = await _orchestrator_request("GET", "/api/v5/dashboard/feature-flags", tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def toggle_feature_flag(flag_name: str, enabled: bool, tenant_id: str = "local") -> str:
    """
    Enable or disable a feature flag in the orchestrator.
    Example: toggle_feature_flag('llm.circuit_breaker', True)
    """
    res = await _orchestrator_request(
        "POST", "/api/v5/dashboard/feature-flags/toggle",
        body={"flag": flag_name, "enabled": enabled},
        tenant_id=tenant_id,
    )
    return json.dumps(res, indent=2)


# ── Capabilities Discovery ─────────────────────────────────────────────────────

@mcp.tool()
async def get_orchestrator_capabilities() -> str:
    """
    Discovery tool: Returns the list of currently available agents,
    active projects, and system health.
    """
    res = await _orchestrator_request("GET", "/api/v1/system/capabilities")
    return json.dumps(res, indent=2)

@mcp.tool()
async def get_opencode_sessions(tenant_id: str = "local") -> str:
    """
    List active OpenCode coding sessions currently running in the orchestrator.
    Returns session IDs, associated tasks, token usage, and files being modified.
    """
    try:
        from services.opencode_client import get_opencode_client
        client = get_opencode_client()
        sessions = await client.list_sessions()
        if not sessions:
            return "No active OpenCode sessions."
        return json.dumps(sessions, indent=2)
    except Exception as e:
        return f"Error fetching OpenCode sessions: {e}"

@mcp.tool()
async def opencode_health() -> str:
    """
    Check OpenCode coding assistant health.
    Returns whether serve mode (HTTP API) or subprocess fallback is active.
    """
    try:
        from services.opencode_client import get_opencode_client
        client = get_opencode_client()
        status = await client.health()
        return json.dumps(status, indent=2)
    except Exception as e:
        return f"OpenCode health check failed: {e}"


if __name__ == "__main__":
    mcp.run()
