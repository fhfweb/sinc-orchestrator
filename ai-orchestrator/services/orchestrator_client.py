from services.streaming.core.config import env_get
"""
Orchestrator Client SDK
=======================
Thin Python client for the SINC Orchestrator HTTP server (streaming_server.py).

Usage (library):
    from services.orchestrator_client import OrchestratorClient
    client = OrchestratorClient("http://localhost:8765", api_key="...")
    print(client.status())
    client.create_task("Fix login bug", agent="AI Engineer")

Usage (CLI):
    python orchestrator_client.py status
    python orchestrator_client.py tasks
    python orchestrator_client.py agents
    python orchestrator_client.py gates
    python orchestrator_client.py approve-gate 2 Fernando
    python orchestrator_client.py create-task "Fix login bug" "AI Engineer"
    python orchestrator_client.py ingest /path/to/project myproject tenant1
    python orchestrator_client.py stream
    python orchestrator_client.py scheduler run
    python orchestrator_client.py lesson record <sig> <fix> success [ctx]
    python orchestrator_client.py lesson query [sig]
    python orchestrator_client.py impact <task_id>
    python orchestrator_client.py plan create <goal> [--agent <name>]
    python orchestrator_client.py plan list
    python orchestrator_client.py plan graph <plan_id>
    python orchestrator_client.py simulate blast <project_path> <file1> [file2 ...]
    python orchestrator_client.py simulate task <task_id> <project_path>
    python orchestrator_client.py simulate history [project_id]
    python orchestrator_client.py entropy scan <project_path> [project_id]
    python orchestrator_client.py entropy report [project_id] [--label critical]
    python orchestrator_client.py entropy seed [project_id] [0.70]
    python orchestrator_client.py twin sync <project_path> [project_id]
    python orchestrator_client.py twin gaps [project_id]
    python orchestrator_client.py twin coupling [project_id]
    python orchestrator_client.py twin status [project_id]
    python orchestrator_client.py twin impact <file_path>
    python orchestrator_client.py twin query "MATCH (n) RETURN n LIMIT 10"

Environment variables:
    ORCHESTRATOR_URL      default: http://localhost:8765
    ORCHESTRATOR_API_KEY  default: (empty — no auth)
"""

import json
import os
import time
import urllib.parse
from typing import Callable

import httpx
from services.http_client import create_sync_resilient_client


class OrchestratorClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        api_key: str = "",
        timeout: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key
        self.timeout  = timeout

    # ──────────────────────────────────────────────
    # INTERNAL HELPERS
    # ──────────────────────────────────────────────

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            h["X-Api-Key"] = self.api_key
        return h

    def _req(self, method: str, path: str, body=None) -> dict:
        url  = f"{self.base_url}{path}"
        try:
            with create_sync_resilient_client(
                service_name="orchestrator-client",
                timeout=self.timeout,
                headers=self._headers(),
            ) as client:
                response = client.request(method.upper(), url, json=body if body is not None else None)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = {"error": str(e)}
            raise RuntimeError(f"HTTP {e.response.status_code} {method} {path}: {detail}") from e

    # ──────────────────────────────────────────────
    # HEALTH / STATUS
    # ──────────────────────────────────────────────

    def health(self) -> dict:
        """Quick liveness check."""
        return self._req("GET", "/health")

    def status(self) -> dict:
        """Full system snapshot: loop + tasks + agents."""
        return self._req("GET", "/status")

    # ──────────────────────────────────────────────
    # TASKS
    # ──────────────────────────────────────────────

    def list_tasks(self) -> dict:
        return self._req("GET", "/tasks")

    def get_task(self, task_id: str) -> dict:
        return self._req("GET", f"/tasks/{task_id}")

    def create_task(
        self,
        title: str,
        description: str = "",
        agent: str | None = None,
        priority: int = 2,
        task_id: str | None = None,
        project_id: str = "",
        requires_review: bool = False,
        depends_on: list | None = None,
        plan_id: str = "",
        verification_required: bool = False,
        red_team_enabled: bool = False,
    ) -> dict:
        """Submit a new task. Returns {"ok": true, "task_id": "...", "status": "..."}"""
        body: dict = {"title": title, "description": description, "priority": priority}
        if agent:
            body["agent"] = agent
        if task_id:
            body["id"] = task_id
        if project_id:
            body["project_id"] = project_id
        if requires_review:
            body["requires_review"] = True
        if depends_on:
            body["dependency_ids"] = depends_on
        if plan_id:
            body["plan_id"] = plan_id
        if verification_required:
            body["verification_required"] = True
        if red_team_enabled:
            body["red_team_enabled"] = True
        return self._req("POST", "/tasks", body)

    def update_task_status(self, task_id: str, status: str, reason: str = "manual-override") -> dict:
        return self._req("PATCH", f"/tasks/{task_id}/status", {"status": status, "reason": reason})

    def replay_task(self, task_id: str) -> dict:
        """Reset a task back to pending for re-execution."""
        return self._req("POST", f"/tasks/{task_id}/replay")

    def review_task(
        self,
        task_id: str,
        action: str,
        decided_by: str = "human",
        feedback: str = "",
    ) -> dict:
        """
        Approve or reject a task in awaiting-review state.
        action: "approve" → done | "reject" → needs-revision
        """
        return self._req("POST", f"/tasks/{task_id}/review", {
            "action":     action,
            "decided_by": decided_by,
            "feedback":   feedback,
        })

    def get_task_context(self, task_id: str) -> dict:
        """Fetch Neo4j graph context for a task (nodes + edges matching task keywords)."""
        return self._req("GET", f"/tasks/{task_id}/context")

    def create_sandbox(
        self,
        task_id: str,
        script: str,
        working_dir: str = "",
        agent_name: str = "",
    ) -> dict:
        """Request a sandbox execution for a task. Returns {"ok": true, "sandbox_id": N}"""
        body: dict = {"script": script}
        if working_dir:
            body["working_dir"] = working_dir
        if agent_name:
            body["agent_name"] = agent_name
        return self._req("POST", f"/tasks/{task_id}/sandbox", body)

    def list_sandboxes(self, task_id: str) -> dict:
        """List all sandbox executions for a task."""
        return self._req("GET", f"/tasks/{task_id}/sandbox")

    def submit_sandbox_result(
        self,
        task_id: str,
        sandbox_id: int,
        status: str,
        output: str = "",
        exit_code: int = 0,
        agent_name: str = "",
    ) -> dict:
        """
        Post sandbox execution result back to the orchestrator.
        status: "passed" | "failed"
        Used by external workers that run sandbox scripts locally.
        """
        body: dict = {"status": status, "output": output, "exit_code": exit_code}
        if agent_name:
            body["agent_name"] = agent_name
        return self._req("POST", f"/tasks/{task_id}/sandbox/{sandbox_id}/result", body)

    # ──────────────────────────────────────────────
    # AGENTS
    # ──────────────────────────────────────────────

    def list_agents(self) -> dict:
        """All agents with workload + reputation metrics."""
        return self._req("GET", "/agents")

    def get_agent(self, agent_name: str) -> dict:
        return self._req("GET", f"/agents/{agent_name}")

    def agent_pending(self, agent_name: str) -> dict:
        """Tasks waiting to be picked up by an external agent."""
        return self._req("GET", f"/agents/{agent_name}/pending")

    def agent_complete(
        self,
        agent_name: str,
        task_id: str,
        status: str = "success",
        summary: str = "",
        files_modified: list | None = None,
        tests_passed: bool = True,
        next_suggested_tasks: list | None = None,
    ) -> dict:
        return self._req("POST", f"/agents/{agent_name}/completion", {
            "task_id":              task_id,
            "status":               status,
            "summary":              summary,
            "files_modified":       files_modified or [],
            "tests_passed":         tests_passed,
            "next_suggested_tasks": next_suggested_tasks or [],
        })

    def agent_heartbeat(
        self,
        agent_name: str,
        task_id: str,
        progress_pct: int = 0,
        current_step: str = "",
    ) -> dict:
        return self._req("POST", f"/agents/{agent_name}/heartbeat", {
            "task_id":      task_id,
            "progress_pct": progress_pct,
            "current_step": current_step,
        })

    # ──────────────────────────────────────────────
    # HUMAN GATES (HITL)
    # ──────────────────────────────────────────────

    def list_gates(self) -> dict:
        return self._req("GET", "/gates")

    def approve_gate(self, gate_id: int, decided_by: str = "human", reason: str = "") -> dict:
        return self._req("POST", f"/gates/{gate_id}/approve", {
            "decided_by": decided_by, "reason": reason,
        })

    def reject_gate(self, gate_id: int, decided_by: str = "human", reason: str = "") -> dict:
        return self._req("POST", f"/gates/{gate_id}/reject", {
            "decided_by": decided_by, "reason": reason,
        })

    # ──────────────────────────────────────────────
    # LOOP
    # ──────────────────────────────────────────────

    def loop_state(self) -> dict:
        return self._req("GET", "/loop/state")

    # ──────────────────────────────────────────────
    # INGEST
    # ──────────────────────────────────────────────

    def ingest(
        self,
        project_path: str,
        project_id: str = "",
        tenant_id: str = "",
        deep: bool = False,
    ) -> dict:
        """
        Trigger async project ingestion pipeline.
        deep=True enables AST parsing + knowledge graph build.
        Returns {"ok": true, "pipeline_id": "INGEST-..."}
        """
        return self._req("POST", "/ingest", {
            "project_path": project_path,
            "project_id":   project_id,
            "tenant_id":    tenant_id,
            "deep":         deep,
        })

    # ──────────────────────────────────────────────
    # LESSONS LEARNED
    # ──────────────────────────────────────────────

    def record_lesson(
        self,
        error_signature: str,
        attempted_fix: str,
        result: str,
        context: str = "",
        confidence: float = 1.0,
        agent_name: str = "",
        task_id: str = "",
        project_id: str = "",
    ) -> dict:
        """
        Record a lessons-learned entry after an error/fix cycle.
        result: "success" | "failure"
        """
        body: dict = {
            "error_signature": error_signature,
            "attempted_fix":   attempted_fix,
            "result":          result,
            "context":         context,
            "confidence":      confidence,
        }
        if agent_name:
            body["agent_name"] = agent_name
        if task_id:
            body["task_id"] = task_id
        if project_id:
            body["project_id"] = project_id
        return self._req("POST", "/lessons", body)

    def query_lessons(
        self,
        error_signature: str = "",
        project_id: str = "",
        limit: int = 5,
    ) -> dict:
        """
        Query past lessons. Pass error_signature to filter by pattern.
        Returns {"lessons": [...]} ordered by confidence DESC.
        """
        params: list[str] = [f"limit={limit}"]
        if error_signature:
            params.append(f"sig={urllib.parse.quote(error_signature)}")
        if project_id:
            params.append(f"project_id={urllib.parse.quote(project_id)}")
        qs = "&".join(params)
        return self._req("GET", f"/lessons?{qs}")

    # ──────────────────────────────────────────────
    # IMPACT RADIUS
    # ──────────────────────────────────────────────

    def get_impact(self, task_id: str) -> dict:
        """
        Transitive Neo4j dependency traversal for the files referenced by task.
        Returns {"task_id": ..., "affected_modules": [...], "depth": N}
        """
        return self._req("GET", f"/tasks/{task_id}/impact")

    # ──────────────────────────────────────────────
    # GLOBAL PLANNER
    # ──────────────────────────────────────────────

    def create_plan(
        self,
        goal: str,
        project_id: str = "",
        agent: str | None = None,
    ) -> dict:
        """
        Submit a natural-language goal for LLM decomposition into tasks.
        Returns {"ok": true, "plan_id": "...", "tasks_created": N, "tasks": [...]}
        """
        body: dict = {"goal": goal}
        if project_id:
            body["project_id"] = project_id
        if agent:
            body["agent"] = agent
        return self._req("POST", "/plan", body)

    def list_plans(self, project_id: str = "") -> dict:
        """List all plans for the current tenant."""
        qs = f"?project_id={urllib.parse.quote(project_id)}" if project_id else ""
        return self._req("GET", f"/plans{qs}")

    def get_plan_graph(self, plan_id: str) -> dict:
        """
        Return the full task DAG for a plan as nodes + directed edges.
        {"plan_id": ..., "goal": ..., "nodes": [...], "edges": [{"source": ..., "target": ...}]}
        """
        return self._req("GET", f"/plans/{plan_id}/graph")

    # ──────────────────────────────────────────────
    # DIGITAL TWIN
    # ──────────────────────────────────────────────

    def twin_sync(self, project_path: str, project_id: str = "") -> dict:
        """Trigger a full Digital Twin sync for a project. Returns sync stats."""
        return self._req("POST", "/twin/sync",
                         {"project_path": project_path, "project_id": project_id})

    def twin_sync_file(self, abs_path: str, project_path: str,
                       project_id: str = "") -> dict:
        """Incremental single-file sync."""
        return self._req("POST", "/twin/sync/file",
                         {"abs_path": abs_path, "project_path": project_path,
                          "project_id": project_id})

    def twin_gaps(self, project_id: str = "") -> dict:
        """Return untested functions, dead code, uncovered files."""
        qs = f"?project_id={urllib.parse.quote(project_id)}" if project_id else ""
        return self._req("GET", f"/twin/gaps{qs}")

    def twin_coupling(self, project_id: str = "", min_dependents: int = 3) -> dict:
        """Return high-coupling hotspots and circular dependency candidates."""
        qs = (f"?project_id={urllib.parse.quote(project_id)}"
              f"&min_dependents={min_dependents}")
        return self._req("GET", f"/twin/coupling{qs}")

    def twin_status(self, project_id: str = "") -> dict:
        """Node + relationship counts in the Digital Twin graph."""
        qs = f"?project_id={urllib.parse.quote(project_id)}" if project_id else ""
        return self._req("GET", f"/twin/status{qs}")

    def twin_query(self, cypher: str, params: dict | None = None) -> dict:
        """Execute a read-only Cypher query against the Digital Twin."""
        return self._req("POST", "/twin/query",
                         {"cypher": cypher, "params": params or {}})

    def twin_impact(self, file_path: str, project_id: str = "") -> dict:
        """Transitive impact radius for a file path."""
        qs = f"?project_id={urllib.parse.quote(project_id)}" if project_id else ""
        safe = urllib.parse.quote(file_path, safe="/")
        return self._req("GET", f"/twin/impact/{safe}{qs}")

    # ──────────────────────────────────────────────
    # ENTROPY SCANNER
    # ──────────────────────────────────────────────

    def entropy_scan(self, project_path: str, project_id: str = "") -> dict:
        """Run a full entropy scan and persist results. Returns summary."""
        return self._req("POST", "/entropy/scan",
                         {"project_path": project_path, "project_id": project_id})

    def entropy_report(self, project_id: str = "", label: str = "") -> dict:
        """Latest per-file entropy scores. label: critical|refactor|watch|healthy"""
        qs = f"?project_id={urllib.parse.quote(project_id)}"
        if label:
            qs += f"&label={label}"
        return self._req("GET", f"/entropy/report{qs}")

    def entropy_trend(self, file_path: str, project_id: str = "") -> dict:
        """Entropy history for one file."""
        qs = (f"?project_id={urllib.parse.quote(project_id)}"
              f"&file={urllib.parse.quote(file_path, safe='/')}")
        return self._req("GET", f"/entropy/trend{qs}")

    def entropy_project_trend(self, project_id: str = "") -> dict:
        """Aggregate entropy trend (time series) for the whole project."""
        qs = f"?project_id={urllib.parse.quote(project_id)}" if project_id else ""
        return self._req("GET", f"/entropy/project-trend{qs}")

    def entropy_velocity(self, project_id: str = "",
                         tenant_id: str = "local", window: int = 5) -> dict:
        """Project entropy velocity, acceleration, trend, and next-scan forecast."""
        qs = (f"?project_id={urllib.parse.quote(project_id)}"
              f"&tenant_id={urllib.parse.quote(tenant_id)}&window={window}")
        return self._req("GET", f"/entropy/velocity{qs}")

    def entropy_file_velocity(self, file_path: str, project_id: str = "",
                              tenant_id: str = "local", window: int = 10) -> dict:
        """Per-file entropy velocity and acceleration."""
        qs = (f"?project_id={urllib.parse.quote(project_id)}"
              f"&tenant_id={urllib.parse.quote(tenant_id)}"
              f"&file={urllib.parse.quote(file_path)}&window={window}")
        return self._req("GET", f"/entropy/file-velocity{qs}")

    def entropy_seed_tasks(self, project_id: str = "",
                           threshold: float = 0.70) -> dict:
        """Create repair tasks for all files above threshold entropy."""
        return self._req("POST", "/entropy/seed-tasks",
                         {"project_id": project_id, "threshold": threshold})

    # ──────────────────────────────────────────────
    # ENGINEERING TIME MACHINE
    # ──────────────────────────────────────────────

    def simulate_change(self, change_spec: dict, project_path: str,
                        project_id: str = "") -> dict:
        """Simulate a change spec. Returns risk_score, recommendation, blast_radius."""
        return self._req("POST", "/simulate/change",
                         {"change_spec": change_spec, "project_path": project_path,
                          "project_id": project_id})

    def simulate_task(self, task_id: str, project_path: str,
                      project_id: str = "") -> dict:
        """Simulate a task by ID (uses task_file_links history)."""
        return self._req("POST", f"/simulate/task/{task_id}",
                         {"project_path": project_path, "project_id": project_id})

    def simulate_plan(self, tasks: list, project_path: str,
                      project_id: str = "") -> dict:
        """Simulate an ordered list of change specs cumulatively."""
        return self._req("POST", "/simulate/plan",
                         {"tasks": tasks, "project_path": project_path,
                          "project_id": project_id})

    def simulate_blast(self, files: list[str], project_path: str,
                       project_id: str = "", max_depth: int = 4) -> dict:
        """Blast radius for a set of files."""
        return self._req("POST", "/simulate/blast",
                         {"files": files, "project_path": project_path,
                          "project_id": project_id, "max_depth": max_depth})

    def simulate_history(self, project_id: str = "", limit: int = 50) -> dict:
        """Recent simulation runs for a project."""
        qs = f"?project_id={urllib.parse.quote(project_id)}&limit={limit}"
        return self._req("GET", f"/simulate/history{qs}")

    # ──────────────────────────────────────────────
    # GITHUB CONNECTOR
    # ──────────────────────────────────────────────

    def connect_github(self, repo_url: str, access_token: str = "",
                       project_id: str = "", branch: str = "main",
                       webhook_secret: str = "") -> dict:
        """Connect a GitHub repository. Returns {job_id, status_url}."""
        return self._req("POST", "/connect/github", {
            "repo_url": repo_url,
            "access_token": access_token,
            "project_id": project_id,
            "branch": branch,
            "webhook_secret": webhook_secret,
        })

    def connect_job_status(self, job_id: str) -> dict:
        """Poll the status of a connect pipeline job."""
        return self._req("GET", f"/connect/jobs/{job_id}")

    def connect_sync(self, project_id: str) -> dict:
        """Re-pull and re-analyse an already-connected repository."""
        return self._req("POST", f"/connect/sync/{urllib.parse.quote(project_id)}")

    def connect_list_repos(self, tenant_id: str = "") -> dict:
        """List all connected repositories."""
        qs = f"?tenant_id={urllib.parse.quote(tenant_id)}" if tenant_id else ""
        return self._req("GET", f"/connect/repos{qs}")

    # ──────────────────────────────────────────────
    # SCHEDULER
    # ──────────────────────────────────────────────

    def scheduler_run(self) -> dict:
        """
        Trigger the reputation-engine auto-assignment pass immediately.
        Returns {"assigned": N} — number of pending tasks that were assigned.
        """
        return self._req("POST", "/scheduler/run")

    # ──────────────────────────────────────────────
    # REPORTS
    # ──────────────────────────────────────────────

    def policy_report(self) -> dict:
        return self._req("GET", "/reports/policy")

    def whiteboard(self) -> dict:
        return self._req("GET", "/whiteboard")

    def dashboard_state(self) -> dict:
        return self._req("GET", "/dashboard/state")

    # ──────────────────────────────────────────────
    # STREAMING (SSE)
    # ──────────────────────────────────────────────

    def stream_events(
        self,
        callback: Callable[[str, dict], None],
        reconnect: bool = True,
        reconnect_delay: float = 5.0,
    ) -> None:
        """
        Connect to SSE /events and call callback(event_type, data) per event.
        Blocks indefinitely. Reconnects on disconnect when reconnect=True.

        Example:
            def on_event(evt_type, data):
                print(f"[{evt_type}] {data}")
            client.stream_events(on_event)
        """
        while True:
            try:
                url = f"{self.base_url}/events"
                headers = {"Accept": "text/event-stream"}
                if self.api_key:
                    headers["X-Api-Key"] = self.api_key
                with create_sync_resilient_client(
                    service_name="orchestrator-client-stream",
                    timeout=self.timeout,
                    headers=headers,
                ) as client:
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        for raw_line in resp.iter_lines():
                            line = str(raw_line or "").strip()
                        if line.startswith("data:"):
                            payload = line[5:].strip()
                            if payload and payload != "{}":
                                try:
                                    evt = json.loads(payload)
                                    callback(
                                        evt.get("type", "unknown"),
                                        evt.get("data", {}),
                                    )
                                except json.JSONDecodeError:
                                    pass
            except Exception as exc:
                if not reconnect:
                    raise
                print(f"[orchestrator-client] stream disconnected ({exc}), "
                      f"reconnecting in {reconnect_delay}s...")
                time.sleep(reconnect_delay)

    # ──────────────────────────────────────────────
    # CONVENIENCE: wait for task completion
    # ──────────────────────────────────────────────

    def wait_for_task(
        self,
        task_id: str,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
    ) -> dict:
        """
        Poll until task reaches a terminal state (done / cancelled / blocked).
        Returns the final task dict.
        """
        terminal = {"done", "cancelled", "blocked-phase-approval", "blocked-lock-conflict",
                    "awaiting-review", "needs-revision", "dead-letter", "blocked-deps"}
        deadline = time.monotonic() + timeout
        while True:
            task = self.get_task(task_id)
            if task.get("status") in terminal:
                return task
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Task {task_id} not terminal after {timeout}s")
            time.sleep(poll_interval)


# ──────────────────────────────────────────────
# CLI ENTRYPOINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    _base = env_get("ORCHESTRATOR_URL", default="http://localhost:8765")
    _key  = env_get("ORCHESTRATOR_API_KEY", default="")
    client = OrchestratorClient(_base, api_key=_key)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    def _print(data):
        print(json.dumps(data, indent=2, default=str))

    if cmd == "status":
        _print(client.status())

    elif cmd == "health":
        _print(client.health())

    elif cmd == "tasks":
        _print(client.list_tasks())

    elif cmd == "agents":
        _print(client.list_agents())

    elif cmd == "gates":
        _print(client.list_gates())

    elif cmd == "loop":
        _print(client.loop_state())

    elif cmd == "policy":
        _print(client.policy_report())

    elif cmd == "dashboard":
        _print(client.dashboard_state())

    elif cmd == "stream":
        print(f"Streaming events from {_base}/events  (Ctrl+C to stop)")
        def _on(evt_type, data):
            print(f"[{evt_type}] {json.dumps(data, default=str)}")
        client.stream_events(_on)

    elif cmd == "approve-gate":
        if len(sys.argv) < 3:
            print("Usage: approve-gate <gate_id> [decided_by]")
            sys.exit(1)
        gate_id    = int(sys.argv[2])
        decided_by = sys.argv[3] if len(sys.argv) > 3 else "human"
        _print(client.approve_gate(gate_id, decided_by=decided_by))

    elif cmd == "reject-gate":
        if len(sys.argv) < 3:
            print("Usage: reject-gate <gate_id> [reason]")
            sys.exit(1)
        _print(client.reject_gate(int(sys.argv[2]), reason=" ".join(sys.argv[3:])))

    elif cmd == "create-task":
        if len(sys.argv) < 3:
            print("Usage: create-task <title> [agent] [description] [--review] [--depends-on ID1,ID2]")
            sys.exit(1)
        title           = sys.argv[2]
        agent           = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else None
        description     = sys.argv[4] if len(sys.argv) > 4 and not sys.argv[4].startswith("--") else ""
        requires_review = "--review" in sys.argv
        depends_on: list = []
        if "--depends-on" in sys.argv:
            idx = sys.argv.index("--depends-on")
            if idx + 1 < len(sys.argv):
                depends_on = sys.argv[idx + 1].split(",")
        _print(client.create_task(title, description=description, agent=agent,
                                  requires_review=requires_review, depends_on=depends_on or None))

    elif cmd == "replay-task":
        if len(sys.argv) < 3:
            print("Usage: replay-task <task_id>")
            sys.exit(1)
        _print(client.replay_task(sys.argv[2]))

    elif cmd == "review-task":
        # review-task <task_id> approve|reject [decided_by] [feedback]
        if len(sys.argv) < 4:
            print("Usage: review-task <task_id> approve|reject [decided_by] [feedback]")
            sys.exit(1)
        action     = sys.argv[3]
        decided_by = sys.argv[4] if len(sys.argv) > 4 else "human"
        feedback   = " ".join(sys.argv[5:]) if len(sys.argv) > 5 else ""
        _print(client.review_task(sys.argv[2], action=action, decided_by=decided_by, feedback=feedback))

    elif cmd == "context":
        # context <task_id>
        if len(sys.argv) < 3:
            print("Usage: context <task_id>")
            sys.exit(1)
        _print(client.get_task_context(sys.argv[2]))

    elif cmd == "sandbox":
        # sandbox <task_id> list
        # sandbox <task_id> run <script>
        if len(sys.argv) < 4:
            print("Usage: sandbox <task_id> list|run [script]")
            sys.exit(1)
        sub = sys.argv[3]
        if sub == "list":
            _print(client.list_sandboxes(sys.argv[2]))
        elif sub == "run":
            script = " ".join(sys.argv[4:]) if len(sys.argv) > 4 else ""
            _print(client.create_sandbox(sys.argv[2], script=script))
        elif sub == "result":
            # sandbox <task_id> result <sandbox_id> passed|failed [exit_code] [output]
            if len(sys.argv) < 6:
                print("Usage: sandbox <task_id> result <sandbox_id> passed|failed [exit_code]")
                sys.exit(1)
            sid       = int(sys.argv[4])
            status    = sys.argv[5]
            exit_code = int(sys.argv[6]) if len(sys.argv) > 6 else (0 if status == "passed" else 1)
            _print(client.submit_sandbox_result(sys.argv[2], sid, status=status, exit_code=exit_code))
        else:
            print("sandbox sub-commands: list, run <script>, result <id> passed|failed [exit_code]")
            sys.exit(1)

    elif cmd == "ingest":
        if len(sys.argv) < 3:
            print("Usage: ingest <project_path> [project_id] [tenant_id] [--deep]")
            sys.exit(1)
        project_path = sys.argv[2]
        project_id   = sys.argv[3] if len(sys.argv) > 3 else ""
        tenant_id    = sys.argv[4] if len(sys.argv) > 4 else ""
        deep         = "--deep" in sys.argv
        _print(client.ingest(project_path, project_id, tenant_id, deep=deep))

    elif cmd == "lesson":
        # lesson record <error_sig> <fix> success|failure [context]
        # lesson query [error_sig] [--limit N]
        if len(sys.argv) < 3:
            print("Usage: lesson record <sig> <fix> success|failure [context]")
            print("       lesson query [sig] [--limit N]")
            sys.exit(1)
        sub = sys.argv[2]
        if sub == "record":
            if len(sys.argv) < 6:
                print("Usage: lesson record <sig> <fix> success|failure [context]")
                sys.exit(1)
            _print(client.record_lesson(
                error_signature=sys.argv[3],
                attempted_fix=sys.argv[4],
                result=sys.argv[5],
                context=" ".join(sys.argv[6:]) if len(sys.argv) > 6 else "",
            ))
        elif sub == "query":
            sig   = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else ""
            limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 5
            _print(client.query_lessons(error_signature=sig, limit=limit))
        else:
            print("lesson sub-commands: record, query")
            sys.exit(1)

    elif cmd == "impact":
        if len(sys.argv) < 3:
            print("Usage: impact <task_id>")
            sys.exit(1)
        _print(client.get_impact(sys.argv[2]))

    elif cmd == "plan":
        # plan create <goal> [--agent <name>]
        # plan list
        if len(sys.argv) < 3:
            print("Usage: plan create <goal> [--agent <name>]")
            print("       plan list")
            sys.exit(1)
        sub = sys.argv[2]
        if sub == "create":
            if len(sys.argv) < 4:
                print("Usage: plan create <goal> [--agent <name>]")
                sys.exit(1)
            agent_idx = sys.argv.index("--agent") if "--agent" in sys.argv else None
            agent     = sys.argv[agent_idx + 1] if agent_idx else None
            # goal is everything after "create" up to the first --flag
            goal_parts = [a for a in sys.argv[3:] if not a.startswith("--")]
            if agent_idx:
                goal_parts = [a for a in goal_parts if a != agent]
            _print(client.create_plan(" ".join(goal_parts), agent=agent))
        elif sub == "list":
            _print(client.list_plans())
        elif sub == "graph":
            if len(sys.argv) < 4:
                print("Usage: plan graph <plan_id>")
                sys.exit(1)
            _print(client.get_plan_graph(sys.argv[3]))
        else:
            print("plan sub-commands: create, list, graph <plan_id>")
            sys.exit(1)

    elif cmd == "twin":
        # twin sync <project_path> [project_id]
        # twin sync-file <abs_path> <project_path> [project_id]
        # twin gaps [project_id]
        # twin coupling [project_id] [min_dependents]
        # twin status [project_id]
        # twin impact <file_path> [project_id]
        # twin query "MATCH (n) ..."
        if len(sys.argv) < 3:
            print("Usage: twin <sub-command> [args]")
            print("  sync <project_path> [project_id]")
            print("  sync-file <abs_path> <project_path> [project_id]")
            print("  gaps [project_id]")
            print("  coupling [project_id] [min_dependents]")
            print("  status [project_id]")
            print("  impact <file_path> [project_id]")
            print('  query "MATCH (n) RETURN n LIMIT 10"')
            sys.exit(1)
        sub = sys.argv[2]
        if sub == "sync":
            pp  = sys.argv[3] if len(sys.argv) > 3 else "."
            pid = sys.argv[4] if len(sys.argv) > 4 else ""
            _print(client.twin_sync(pp, pid))
        elif sub == "sync-file":
            if len(sys.argv) < 5:
                print("Usage: twin sync-file <abs_path> <project_path> [project_id]")
                sys.exit(1)
            _print(client.twin_sync_file(sys.argv[3], sys.argv[4],
                                         sys.argv[5] if len(sys.argv) > 5 else ""))
        elif sub == "gaps":
            _print(client.twin_gaps(sys.argv[3] if len(sys.argv) > 3 else ""))
        elif sub == "coupling":
            pid     = sys.argv[3] if len(sys.argv) > 3 else ""
            min_dep = int(sys.argv[4]) if len(sys.argv) > 4 else 3
            _print(client.twin_coupling(pid, min_dep))
        elif sub == "status":
            _print(client.twin_status(sys.argv[3] if len(sys.argv) > 3 else ""))
        elif sub == "impact":
            if len(sys.argv) < 4:
                print("Usage: twin impact <file_path> [project_id]")
                sys.exit(1)
            pid = sys.argv[4] if len(sys.argv) > 4 else ""
            _print(client.twin_impact(sys.argv[3], pid))
        elif sub == "query":
            if len(sys.argv) < 4:
                print('Usage: twin query "MATCH (n) RETURN n"')
                sys.exit(1)
            cypher = " ".join(sys.argv[3:])
            _print(client.twin_query(cypher))
        else:
            print("twin sub-commands: sync, sync-file, gaps, coupling, status, impact, query")
            sys.exit(1)

    elif cmd == "simulate":
        # simulate blast <project_path> <file1> [file2 ...]
        # simulate task <task_id> <project_path> [project_id]
        # simulate history [project_id]
        if len(sys.argv) < 3:
            print("Usage: simulate <sub-command> [args]")
            print("  blast <project_path> <file1> [file2 ...]")
            print("  task  <task_id> <project_path> [project_id]")
            print("  history [project_id]")
            sys.exit(1)
        sub = sys.argv[2]
        if sub == "blast":
            if len(sys.argv) < 5:
                print("Usage: simulate blast <project_path> <file1> [file2 ...]")
                sys.exit(1)
            pp    = sys.argv[3]
            files = sys.argv[4:]
            result = client.simulate_blast(files, pp)
            _print(result)
            # Human-readable summary
            print(f"\nRisk: {result.get('risk_label','?').upper()} "
                  f"({result.get('risk_score', 0):.2f}) — "
                  f"{result.get('file_count', 0)} files, "
                  f"{result.get('test_count', 0)} tests affected")
        elif sub == "task":
            if len(sys.argv) < 5:
                print("Usage: simulate task <task_id> <project_path> [project_id]")
                sys.exit(1)
            tid = sys.argv[3]
            pp  = sys.argv[4]
            pid = sys.argv[5] if len(sys.argv) > 5 else ""
            result = client.simulate_task(tid, pp, pid)
            _print(result)
            if "risk_label" in result:
                print(f"\nRisk: {result['risk_label'].upper()} ({result.get('risk_score',0):.2f})"
                      f" — {result.get('recommendation','?')}")
        elif sub == "history":
            pid = sys.argv[3] if len(sys.argv) > 3 else ""
            _print(client.simulate_history(pid))
        else:
            print("simulate sub-commands: blast, task, history")
            sys.exit(1)

    elif cmd == "entropy":
        # entropy scan <project_path> [project_id]
        # entropy report [project_id] [--label critical|refactor|watch|healthy]
        # entropy trend <file_path> [project_id]
        # entropy project-trend [project_id]
        # entropy seed [project_id] [threshold]
        if len(sys.argv) < 3:
            print("Usage: entropy <sub-command> [args]")
            print("  scan <project_path> [project_id]")
            print("  report [project_id] [--label critical|refactor|watch|healthy]")
            print("  trend <file_path> [project_id]")
            print("  project-trend [project_id]")
            print("  seed [project_id] [threshold]")
            sys.exit(1)
        sub = sys.argv[2]
        if sub == "scan":
            pp  = sys.argv[3] if len(sys.argv) > 3 else "."
            pid = sys.argv[4] if len(sys.argv) > 4 else ""
            _print(client.entropy_scan(pp, pid))
        elif sub == "report":
            pid   = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else ""
            label = ""
            if "--label" in sys.argv:
                label = sys.argv[sys.argv.index("--label") + 1]
            _print(client.entropy_report(pid, label))
        elif sub == "trend":
            if len(sys.argv) < 4:
                print("Usage: entropy trend <file_path> [project_id]")
                sys.exit(1)
            pid = sys.argv[4] if len(sys.argv) > 4 else ""
            _print(client.entropy_trend(sys.argv[3], pid))
        elif sub == "project-trend":
            pid = sys.argv[3] if len(sys.argv) > 3 else ""
            _print(client.entropy_project_trend(pid))
        elif sub == "seed":
            pid       = sys.argv[3] if len(sys.argv) > 3 else ""
            threshold = float(sys.argv[4]) if len(sys.argv) > 4 else 0.70
            _print(client.entropy_seed_tasks(pid, threshold))
        else:
            print("entropy sub-commands: scan, report, trend, project-trend, seed")
            sys.exit(1)

    elif cmd == "scheduler":
        sub = sys.argv[2] if len(sys.argv) > 2 else "run"
        if sub == "run":
            _print(client.scheduler_run())
        else:
            print("scheduler sub-commands: run")
            sys.exit(1)

    elif cmd == "connect":
        # connect github <repo_url> [project_id] [--token TOKEN] [--branch BRANCH] [--webhook-secret SECRET]
        # connect sync <project_id>
        # connect repos [--tenant TENANT_ID]
        # connect jobs <job_id>
        if len(sys.argv) < 3:
            print("Usage: connect <sub-command> [args]")
            print("  github <repo_url> [project_id] [--token TOKEN] [--branch BRANCH] [--webhook-secret SECRET]")
            print("  sync   <project_id>")
            print("  repos  [--tenant TENANT_ID]")
            print("  jobs   <job_id>")
            sys.exit(1)
        sub = sys.argv[2]
        if sub == "github":
            if len(sys.argv) < 4:
                print("Usage: connect github <repo_url> [project_id] [--token TOKEN] [--branch BRANCH]")
                sys.exit(1)
            repo_url   = sys.argv[3]
            project_id = sys.argv[4] if len(sys.argv) > 4 and not sys.argv[4].startswith("--") else ""
            token      = sys.argv[sys.argv.index("--token") + 1] if "--token" in sys.argv else ""
            branch     = sys.argv[sys.argv.index("--branch") + 1] if "--branch" in sys.argv else "main"
            wh_secret  = sys.argv[sys.argv.index("--webhook-secret") + 1] if "--webhook-secret" in sys.argv else ""
            result = client.connect_github(repo_url, access_token=token,
                                           project_id=project_id, branch=branch,
                                           webhook_secret=wh_secret)
            _print(result)
            job_id = result.get("job_id")
            if job_id:
                print(f"\nPipeline started — job_id: {job_id}")
                print(f"Poll status:  python orchestrator_client.py connect jobs {job_id}")
                print(f"Stream logs:  {_base}/connect/jobs/{job_id}/stream")
        elif sub == "sync":
            if len(sys.argv) < 4:
                print("Usage: connect sync <project_id>")
                sys.exit(1)
            _print(client.connect_sync(sys.argv[3]))
        elif sub == "repos":
            tenant = sys.argv[sys.argv.index("--tenant") + 1] if "--tenant" in sys.argv else ""
            _print(client.connect_list_repos(tenant))
        elif sub == "jobs":
            if len(sys.argv) < 4:
                print("Usage: connect jobs <job_id>")
                sys.exit(1)
            _print(client.connect_job_status(sys.argv[3]))
        else:
            print("connect sub-commands: github, sync, repos, jobs")
            sys.exit(1)

    elif cmd == "wait":
        if len(sys.argv) < 3:
            print("Usage: wait <task_id> [timeout_seconds]")
            sys.exit(1)
        timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0
        _print(client.wait_for_task(sys.argv[2], timeout=timeout))

    else:
        print("Commands: status, health, tasks, agents, gates, loop, policy, dashboard,")
        print("          stream, approve-gate <id>, reject-gate <id>,")
        print("          create-task <title> [agent] [desc] [--review], replay-task <id>,")
        print("          review-task <id> approve|reject [decided_by] [feedback],")
        print("          context <task_id>, impact <task_id>,")
        print("          sandbox <task_id> list|run [script]|result <id> passed|failed,")
        print("          lesson record <sig> <fix> success|failure [ctx],")
        print("          lesson query [sig] [--limit N],")
        print("          plan create <goal> [--agent <name>], plan list, plan graph <id>,")
        print("          ingest <path> [project_id] [tenant_id] [--deep],")
        print("          twin sync|sync-file|gaps|coupling|status|impact|query,")
        print("          entropy scan|report|trend|project-trend|seed,")
        print("          simulate blast|task|history,")
        print("          scheduler run,")
        print("          connect github <repo_url> [project_id] [--token] [--branch],")
        print("          connect sync <project_id>, connect repos, connect jobs <job_id>,")
        print("          wait <task_id> [timeout]")
        sys.exit(1)
