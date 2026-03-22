from services.streaming.core.config import env_get
"""
SINC Orchestrator Agent Worker v2
Hybrid, agnostic autonomous agent daemon.
Enforced Service-Only Mode (P0 Architecture).
"""

import os
import sys
import json
import logging
import asyncio
import threading
import subprocess
import time
import traceback
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from httpx import QueryParams

from services.streaming.core.log_handler import setup_canonical_logging

log = logging.getLogger("agent-worker")
# Canonical logging for the Python control plane (MIG-P5-004)
setup_canonical_logging("agent-worker")

def _log(msg: str):
    """Standard logging (now captured by setup_canonical_logging)."""
    log.info(msg)
    # Legacy print for stdout visibility in Docker logs
    print(f"[{datetime.now(timezone.utc).isoformat()}] [agent-worker] {msg}", flush=True)

try:
    from tenacity import (
        retry, stop_after_attempt, wait_exponential,
        retry_if_exception_type, RetryError,
    )
    _HAS_TENACITY = True
except ImportError:
    _HAS_TENACITY = False

try:
    from services.event_store import emit_task_completed as _es_completed, emit_task_failed as _es_failed
    _HAS_EVENT_STORE = True
except ImportError:
    _HAS_EVENT_STORE = False

# Docker SDK - preferred sandbox driver
try:
    import docker as _docker_sdk
    _HAS_DOCKER = True
except ImportError:
    _docker_sdk = None
    _HAS_DOCKER = False
_ALLOW_HOST_SANDBOX_FALLBACK = env_get("ALLOW_HOST_SANDBOX_FALLBACK", default="0") == "1"

# Ray - optional parallel task execution
_HAS_RAY    = False
_ray_module = None

def _ray_init() -> bool:
    global _HAS_RAY, _ray_module
    if not (env_get("RAY_ENABLED", default="0") == "1"):
        return False
    if _HAS_RAY:
        return True
    try:
        import ray as _ray
        address = env_get("RAY_ADDRESS", default="auto")
        _ray.init(address=address, ignore_reinit_error=True, logging_level="warning", log_to_driver=False)
        _ray_module = _ray
        _HAS_RAY = True
        return True
    except Exception as e:
        _log(f"Ray not available ({e}) - serial mode")
        return False

# Config
WORKSPACE_PATH = env_get("AGENT_WORKSPACE", default="/workspace")
WORKSPACE      = Path(WORKSPACE_PATH).resolve()
POLL_INTERVAL  = int(env_get("AGENT_POLL_INTERVAL", default="15"))
AGENT_NAME     = env_get("AGENT_NAME", default="agent-worker")
PROJECT_ID     = env_get("PROJECT_ID", default="")
TENANT_ID      = env_get("TENANT_ID", default="local")
EVENT_PUSH_ENABLED = env_get("EVENT_PUSH_ENABLED", default="0") == "1"
MEMORY_AUDITOR_ENABLED = env_get("MEMORY_AUDITOR_ENABLED", default="0") == "1"
MEMORY_AUDITOR_EVERY_N_TASKS = max(0, int(env_get("MEMORY_AUDITOR_EVERY_N_TASKS", default="0")))

_task_event = threading.Event()

# HTTP transport
ORCHESTRATOR_URL     = env_get("ORCHESTRATOR_URL", default="").rstrip("/")
ORCHESTRATOR_API_KEY = env_get("ORCHESTRATOR_API_KEY", default="")
HTTP_MODE = bool(ORCHESTRATOR_URL)
if "ALLOW_HOST_SANDBOX_FALLBACK" not in os.environ:
    _ALLOW_HOST_SANDBOX_FALLBACK = not HTTP_MODE

# Monitoring
LOGS_DIR = WORKSPACE / "ai-orchestrator" / "logs"

# ── Utilities ────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

# ── HTTP transport ───────────────────────────────────────────────────────────
from services.event_bus import get_event_bus
from services.http_client import create_resilient_client, create_sync_resilient_client

class AgentWorker:
    def __init__(self, name: str, tenant_id: str, project_id: str = ""):
        self.name = name
        self.tenant_id = tenant_id
        self.project_id = project_id or PROJECT_ID
        self.http_client: Optional[Any] = None
        self.bus: Optional[Any] = None
        self.workspace = WORKSPACE
        self.poll_interval = POLL_INTERVAL

    async def initialize(self):
        if not ORCHESTRATOR_URL:
            raise RuntimeError("ORCHESTRATOR_URL is required for worker runtime.")
        headers = {"X-API-Key": ORCHESTRATOR_API_KEY} if ORCHESTRATOR_API_KEY else None
        self.http_client = create_resilient_client(
            service_name="agent-worker",
            base_url=ORCHESTRATOR_URL,
            headers=headers,
        )
        self.bus = await get_event_bus()
        _log(f"AgentWorker {self.name} initialized for tenant {self.tenant_id} [Resilient Stack]")

    async def emit_event(self, event_type: str, payload: dict):
        if not self.bus:
            return
        await self.bus.emit(
            f"agent:{self.name}", 
            {
                "type": event_type,
                "agent": self.name,
                "tenant_id": self.tenant_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **payload
            },
            stream=True
        )
        # Also publish to the telemetry channel for the dashboard
        await self.bus.publish(f"telemetry:{self.tenant_id}", {
            "type": "agent_event",
            "agent": self.name,
            "event": event_type,
            **payload
        }, use_stream=False)

    async def send_heartbeat(self, task_id: str, pct: int, step: str, swarm_data: dict = None):
        payload = {
            "task_id": task_id,
            "progress_pct": pct,
            "current_step": step,
            "agent": self.name
        }
        if swarm_data:
            payload["swarm_data"] = swarm_data

        # 1. Real-time EventBus (Pub/Sub + Stream)
        await self.emit_event("heartbeat", payload)

        # 2. Legacy HTTP callback for compatibility
        try:
            await self.http_client.post(f"/agents/{self.name}/heartbeat", json=payload)
        except Exception as e:
            _log(f"HTTP Heartbeat failed: {e}")

    async def report_completion(self, task_id: str, status: str, summary: str, files: list, backend: str):
        payload = {
            "task_id": task_id,
            "status": status,
            "summary": summary,
            "files_modified": files,
            "backend_used": backend,
            "agent": self.name
        }
        
        # 1. Real-time EventBus
        await self.emit_event("completion", payload)

        # 2. Legacy HTTP callback
        try:
            await self.http_client.post(f"/agents/{self.name}/completion", json=payload)
        except Exception as e:
            _log(f"HTTP Completion failed: {e}")

    async def api(self, method: str, path: str, body: dict = None) -> dict:
        resp = await self.http_client.request(method, path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def fetch_task_context(self, task_id: str) -> dict:
        try:
            return await self.api("GET", f"/tasks/{task_id}/context")
        except Exception as e:
            _log(f"fetch_task_context_failed: {e}")
            return {}

    async def fetch_task_debugger(self, task_id: str) -> dict:
        try:
            return await self.api("GET", f"/api/v5/dashboard/task-debugger/{task_id}")
        except Exception as e:
            _log(f"fetch_task_debugger_failed: {e}")
            return {}

    async def fetch_lessons(self, error_signature: str = "", limit: int = 5) -> str:
        try:
            params = f"limit={limit}"
            if error_signature:
                params += "&" + str(QueryParams({"sig": error_signature}))
            resp = await self.api("GET", f"/lessons?{params}")
            lessons = resp.get("lessons", [])
            if not lessons: return ""
            parts = ["LESSONS LEARNED:"]
            for i, l in enumerate(lessons[:limit], 1):
                res = l.get("result", "?")
                fix = l.get("attempted_fix", "")
                parts.append(f"  {i}. [{res.upper()}] fix={fix!r}")
            return "\n".join(parts)
        except Exception as e:
            _log(f"fetch_lessons_failed: {e}")
            return ""

    async def build_preflight_context(self, task_id: str, task: dict, context_limit: int = 10) -> str:
        return await _build_preflight_context(task_id, task, context_limit=context_limit)

    async def execute_pending_sandboxes(self, task_id: str) -> None:
        await _execute_pending_sandboxes(self, task_id)

    async def run_task(self, runner, task: dict) -> None:
        await _run_task(self, runner, task)

    async def perform_post_task_actions(self, runner, task, result):
        await asyncio.to_thread(_perform_post_task_actions, runner, task, result)


def _resolve_url(path: str) -> str:
    if str(path).startswith("http://") or str(path).startswith("https://"):
        return str(path)
    if not ORCHESTRATOR_URL:
        raise RuntimeError("ORCHESTRATOR_URL is required for HTTP transport")
    return f"{ORCHESTRATOR_URL}/{str(path).lstrip('/')}"


def _api(method: str, path: str, body: dict | None = None) -> dict:
    url = _resolve_url(path)
    headers = {
        "Accept": "application/json",
    }
    if ORCHESTRATOR_API_KEY:
        headers["X-API-Key"] = ORCHESTRATOR_API_KEY
    with create_sync_resilient_client(
        service_name="agent-worker-sync",
        headers=headers,
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        response = client.request(method.upper(), url, json=body)
        response.raise_for_status()
        if not response.content:
            return {}
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return {"raw": response.text}


def _db():
    raise RuntimeError("legacy DB mode is unavailable in the canonical worker runtime")


def _http_complete(task_id: str, status: str, summary: str, files_modified: list[str], backend_used: str):
    payload = {
        "task_id": task_id,
        "status": status,
        "summary": summary,
        "files_modified": files_modified,
        "backend_used": backend_used,
        "agent": AGENT_NAME,
    }
    return _api("POST", f"/agents/{AGENT_NAME}/completion", payload)


def update_task_in_db(
    task_id: str,
    status: str,
    agent_name: str,
    *,
    summary: str = "",
    files_modified: list[str] | None = None,
    backend_used: str = "",
):
    if HTTP_MODE:
        return _http_complete(task_id, status, summary, files_modified or [], backend_used)
    return _db()


def _fetch_task_context(task_id: str) -> dict | str:
    try:
        return _api("GET", f"/tasks/{task_id}/context")
    except Exception as exc:
        _log(f"fetch_task_context_failed: {exc}")
        return {}


def _fetch_task_debugger(task_id: str) -> dict:
    try:
        return _api("GET", f"/api/v5/dashboard/task-debugger/{task_id}")
    except Exception as exc:
        _log(f"fetch_task_debugger_failed: {exc}")
        return {}


def _fetch_lessons(error_signature: str = "", limit: int = 5) -> str:
    try:
        params = f"limit={limit}"
        if error_signature:
            params += "&" + str(QueryParams({"sig": error_signature}))
        resp = _api("GET", f"/lessons?{params}")
        lessons = resp.get("lessons", [])
        if not lessons:
            return ""
        parts = ["LESSONS LEARNED:"]
        for index, lesson in enumerate(lessons[:limit], start=1):
            result = lesson.get("result", "?")
            fix = lesson.get("attempted_fix", "")
            parts.append(f"  {index}. [{result.upper()}] fix={fix!r}")
        return "\n".join(parts)
    except Exception as exc:
        _log(f"fetch_lessons_failed: {exc}")
        return ""


def _derive_memory_queries(task: dict, api_context: dict, debugger: dict) -> list[dict]:
    queries: list[dict] = []
    title = str(task.get("title") or task.get("description") or "").strip()
    task_type = str(task.get("task_type") or "generic").strip()
    if title:
        queries.append({"query": f"{task_type} {title}", "task_type": task_type})

    seen_files = _collect_candidate_files(api_context, debugger)
    for path in seen_files[:3]:
        queries.append(
            {
                "query": f"{task_type} file {path}",
                "task_type": task_type,
                "file_path": path,
            }
        )

    incidents = (debugger.get("reasoning") or {}).get("incidents") or []
    for incident in incidents[:2]:
        summary = str(incident.get("summary") or incident.get("category") or "").strip()
        if summary:
            queries.append(
                {
                    "query": f"{task_type} incident {summary}",
                    "task_type": task_type,
                    "incident_family": summary.split()[0].lower(),
                }
            )
    return queries[:6]


def _collect_candidate_files(api_context: dict, debugger: dict, *, limit: int = 5) -> list[str]:
    files: list[str] = []
    context_payload = debugger.get("context") or {}
    files.extend(context_payload.get("files_affected") or [])
    files.extend(context_payload.get("source_modules") or [])
    for node in api_context.get("nodes") or []:
        path = node.get("path") or node.get("name")
        if path:
            files.append(path)

    seen_files = []
    for path in files:
        normalized = str(path or "").strip().replace("\\", "/")
        if normalized and normalized not in seen_files:
            seen_files.append(normalized)
    return seen_files[:limit]


def _fetch_active_memory_brief(task: dict, api_context: dict, debugger: dict) -> str:
    try:
        from services.local_agent_runner import _execute_tool, WORKSPACE as RUNNER_WORKSPACE
    except Exception as exc:
        _log(f"  active_memory_runner_unavailable: {exc}")
        return ""

    queries = _derive_memory_queries(task, api_context, debugger)
    if not queries:
        return ""

    hits = []
    seen = set()
    for query in queries:
        try:
            raw = _execute_tool("memory_search", {**query, "top_k": 3}, RUNNER_WORKSPACE)
            payload = json.loads(raw)
        except Exception as exc:
            _log(f"  active_memory_search_failed: {exc}")
            continue
        for item in payload.get("results") or []:
            content = str(item.get("content") or "").strip()
            if not content or content in seen:
                continue
            seen.add(content)
            hits.append(item)
            if len(hits) >= 5:
                break
        if len(hits) >= 5:
            break

    if not hits:
        return ""

    parts = ["ACTIVE MEMORY:"]
    for index, item in enumerate(hits, start=1):
        metadata = item.get("metadata") or {}
        note = content = str(item.get("content") or "").strip().replace("\n", " ")
        if len(note) > 220:
            note = note[:217] + "..."
        extra = []
        if metadata.get("task_type"):
            extra.append(f"type={metadata['task_type']}")
        if metadata.get("incident_family"):
            extra.append(f"incident={metadata['incident_family']}")
        if metadata.get("files"):
            extra.append(f"file={metadata['files'][0]}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        parts.append(f"  {index}. {note}{suffix}")
    return "\n".join(parts)


def _fetch_reactivation_hints(task: dict, api_context: dict, debugger: dict) -> str:
    task_type = str(task.get("task_type") or "generic").strip()
    project_id = str(task.get("project_id") or PROJECT_ID or "").strip()
    files = _collect_candidate_files(api_context, debugger, limit=3)
    incidents = (debugger.get("reasoning") or {}).get("incidents") or []
    incident_names = []
    for incident in incidents:
        raw = str(incident.get("summary") or incident.get("category") or "").strip()
        if raw:
            incident_names.append(raw.split()[0].lower())

    file_candidates = files[:2] or [""]
    incident_candidates = incident_names[:2] or [""]

    hints = []
    seen = set()
    for file_path in file_candidates:
        for incident_family in incident_candidates:
            try:
                query = (
                    "/cognitive/memory/reactivation?"
                    + str(
                        QueryParams(
                            {
                                "project_id": project_id,
                                "task_type": task_type,
                                "file_path": file_path,
                                "incident_family": incident_family,
                                "limit": 2,
                            }
                        )
                    )
                )
                resp = _api("GET", query)
            except Exception as exc:
                _log(f"  reactivation_hint_fetch_failed: {exc}")
                continue
            for item in resp.get("items") or []:
                summary = str(item.get("summary") or "").strip()
                if not summary or summary in seen:
                    continue
                seen.add(summary)
                hints.append(item)
                if len(hints) >= 4:
                    break
            if len(hints) >= 4:
                break
        if len(hints) >= 4:
            break

    if not hints:
        return ""

    parts = ["ACTIVE REACTIVATION HINTS:"]
    for index, item in enumerate(hints, start=1):
        hint_kind = str(item.get("hint_kind") or "hint")
        incident_family = str(item.get("incident_family") or "").strip()
        file_path = str(item.get("file_path") or "").strip()
        context = []
        if incident_family:
            context.append(f"incident={incident_family}")
        if file_path:
            context.append(f"file={file_path}")
        if item.get("match_score"):
            context.append(f"score={item['match_score']}")
        suffix = f" ({', '.join(context)})" if context else ""
        parts.append(f"  {index}. [{hint_kind}] {str(item.get('summary') or '').strip()}{suffix}")
    return "\n".join(parts)


def _fetch_entropy_risk_brief(task: dict, api_context: dict, debugger: dict) -> str:
    file_candidates = _collect_candidate_files(api_context, debugger, limit=5)
    if not file_candidates:
        return ""
    project_id = str(task.get("project_id") or PROJECT_ID or "").strip()
    try:
        query = (
            "/entropy/risk-context?"
            + str(
                QueryParams(
                    {
                        "project_id": project_id,
                        "files": ",".join(file_candidates),
                        "limit": 5,
                    }
                )
            )
        )
        payload = _api("GET", query)
    except Exception as exc:
        _log(f"  entropy_risk_fetch_failed: {exc}")
        return ""

    rows = payload.get("files") or []
    profile = str(payload.get("profile") or "normal").strip().lower()
    if not rows and profile == "normal":
        return ""

    instructions = list(payload.get("recommendations") or [])
    if profile in {"elevated", "guarded", "extreme"}:
        instructions.append("Prefer minimal, reversible edits and checkpoint before broad follow-up changes.")
    parts = [f"EXECUTION RISK PROFILE: {profile.upper()}"]
    for index, row in enumerate(rows[:5], start=1):
        file_path = str(row.get("file_path") or "").strip()
        label = str(row.get("label") or "unknown").strip()
        score = float(row.get("entropy_score") or 0.0)
        complexity = int(row.get("complexity") or 0)
        coupling = int(row.get("coupling") or 0)
        parts.append(
            f"  {index}. {file_path} - entropy={score:.2f} label={label} complexity={complexity} coupling={coupling}"
        )
    if instructions:
        parts.append("CAUTION:")
        for index, item in enumerate(instructions[:4], start=1):
            parts.append(f"  {index}. {str(item).strip()}")
    return "\n".join(parts)


async def _build_preflight_context(task_id: str, task: dict, context_limit: int = 10) -> str:
    api_context, lessons_text, debugger_payload = await asyncio.gather(
        asyncio.to_thread(_fetch_task_context, task_id),
        asyncio.to_thread(_fetch_lessons, "", 5),
        asyncio.to_thread(_fetch_task_debugger, task_id),
    )

    if not isinstance(api_context, dict):
        api_context = {"nodes": [], "enriched_prompt": "", "legacy_context": str(api_context or "")}
    if not isinstance(debugger_payload, dict):
        debugger_payload = {}

    if not api_context.get("enriched_prompt"):
        try:
            from services.cognitive_orchestrator import prepare_execution_context

            fallback_context = await prepare_execution_context(
                task=task,
                agent_name=task.get("assigned_agent") or AGENT_NAME,
                tenant_id=task.get("tenant_id") or TENANT_ID,
            )
            api_context["enriched_prompt"] = fallback_context.get("enriched_system_prompt", "")
            api_context.setdefault("intelligence", fallback_context.get("intelligence", {}))
        except Exception as exc:
            _log(f"preflight_cognitive_fallback_failed: {exc}")

    sections: list[str] = []
    enriched_prompt = str(api_context.get("enriched_prompt") or "").strip()
    if enriched_prompt:
        sections.append(f"=== COGNITIVE EXECUTION BRIEF ===\n{enriched_prompt}")

    if lessons_text:
        sections.append(str(lessons_text))

    active_memory, reactivation_hints, entropy_risk = await asyncio.gather(
        asyncio.to_thread(_fetch_active_memory_brief, task, api_context, debugger_payload),
        asyncio.to_thread(_fetch_reactivation_hints, task, api_context, debugger_payload),
        asyncio.to_thread(_fetch_entropy_risk_brief, task, api_context, debugger_payload),
    )
    for block in (active_memory, reactivation_hints, entropy_risk):
        if block:
            sections.append(str(block))

    legacy_context = str(api_context.get("legacy_context") or "").strip()
    if legacy_context:
        sections.append(legacy_context)

    nodes = api_context.get("nodes") or []
    if nodes:
        parts = ["CODEBASE CONTEXT (Structural Nodes):"]
        for node in nodes[:context_limit]:
            labels = node.get("labels", [])
            label = "/".join(labels) if isinstance(labels, list) and labels else "Node"
            path = node.get("path") or node.get("name", "?")
            parts.append(f"  [{label}] {path}")
        sections.append("\n".join(parts))

    return "\n\n".join(part for part in sections if part)


def _perform_post_task_actions(runner, task, result):
    try:
        from services.local_agent_runner import _execute_tool, WORKSPACE as RUNNER_WORKSPACE
    except Exception as exc:
        _log(f"post_task_runner_unavailable: {exc}")
        return {}

    workspace = Path(RUNNER_WORKSPACE)
    tenant_id = str(task.get("tenant_id") or TENANT_ID or "local").strip()
    task_id = str(task.get("id") or task.get("task_id") or "unknown").strip()
    task_type = str(task.get("task_type") or "generic").strip()
    agent_name = str(task.get("assigned_agent") or AGENT_NAME).strip()
    files_modified = list(getattr(result, "files_modified", []) or [])
    summary = str(getattr(result, "summary", "") or "").strip()
    error = str(getattr(result, "error", "") or "").strip()
    backend_used = str(getattr(result, "backend_used", "") or "").strip()
    status = str(getattr(result, "status", "") or "").strip()

    reflection_payload = {
        "goal": str(task.get("title") or task.get("description") or task_id),
        "action_taken": summary or error or status,
        "result": {
            "status": status,
            "summary": summary,
            "error": error,
            "backend_used": backend_used,
            "files_modified": files_modified,
        },
    }

    reflection_data: dict[str, Any] = {}
    try:
        raw_reflection = _execute_tool("self_reflect", reflection_payload, workspace)
        if raw_reflection:
            reflection_data = json.loads(raw_reflection)
    except Exception as exc:
        _log(f"post_task_reflection_failed: {exc}")

    incident_family = str(
        reflection_data.get("incident_family")
        or reflection_data.get("memory_candidate", {}).get("incident_family")
        or _classify_execution_error(RuntimeError(error or summary or status), step="post_task").get("category")
    ).strip()
    validation_status = str(reflection_data.get("validation_status") or ("validated" if status == "done" else "needs_review")).strip()

    common_metadata = {
        "task_id": task_id,
        "task_type": task_type,
        "agent_name": agent_name,
        "backend_used": backend_used,
        "status": status,
        "validation_status": validation_status,
        "files": files_modified,
        "incident_family": incident_family,
    }

    memory_writes: list[dict[str, Any]] = [
        {
            "key": f"task-outcome:{tenant_id}:{task_id}",
            "content": summary or error or f"Task {task_id} completed with status {status}",
            "task_id": task_id,
            "task_type": task_type,
            "agent_name": agent_name,
            "files": files_modified,
            "incident_family": incident_family,
            "metadata": common_metadata,
        }
    ]

    for file_path in files_modified[:5]:
        memory_writes.append(
            {
                "key": f"task-file:{tenant_id}:{task_id}:{file_path}",
                "content": f"{summary or error or status} [file={file_path}]",
                "task_id": task_id,
                "task_type": task_type,
                "agent_name": agent_name,
                "files": [file_path],
                "incident_family": incident_family,
                "metadata": {**common_metadata, "file_path": file_path},
            }
        )

    memory_candidate = reflection_data.get("memory_candidate") or {}
    if bool(memory_candidate.get("should_persist")):
        memory_writes.append(
            {
                "key": f"task-pattern:{tenant_id}:{task_id}",
                "content": str(memory_candidate.get("summary") or summary or error or status),
                "task_id": task_id,
                "task_type": task_type,
                "agent_name": agent_name,
                "files": files_modified,
                "incident_family": str(memory_candidate.get("incident_family") or incident_family),
                "metadata": {
                    **common_metadata,
                    "tags": list(memory_candidate.get("tags") or []),
                    "evidence": list(reflection_data.get("evidence") or []),
                },
            }
        )

    for payload in memory_writes:
        try:
            _execute_tool("memory_write", payload, workspace)
        except Exception as exc:
            _log(f"post_task_memory_write_failed: key={payload.get('key')} error={exc}")

    return {
        "reflection": reflection_data,
        "memory_writes": memory_writes,
    }


async def _execute_pending_sandboxes(worker: AgentWorker, task_id: str) -> None:
    try:
        resp = await worker.api("GET", f"/tasks/{task_id}/sandbox")
        for sandbox in resp.get("sandboxes", []):
            if sandbox.get("status") == "pending":
                result_status, result_output, result_exit = await _safe_execute(
                    sandbox["script"], sandbox.get("working_dir", "")
                )
                await worker.api(
                    "POST",
                    f"/tasks/{task_id}/sandbox/{sandbox['id']}/result",
                    {
                        "status": result_status,
                        "output": result_output,
                        "exit_code": result_exit,
                        "agent_name": worker.name,
                    },
                )
    except Exception as exc:
        _log(f"Sandbox error: {exc}")


async def _run_opencode_task(worker: AgentWorker, task: dict, context: str):
    """Dispatch a task to the OpenCode coding assistant and return an ExecutionResult."""
    from services.local_agent_runner import ExecutionResult as _ExecResult
    from services.opencode_client import (
        get_opencode_client, OPENCODE_ENABLED,
        OPENCODE_PROVIDER_CODING, OPENCODE_MODEL_CODING,
    )

    if not OPENCODE_ENABLED:
        return _ExecResult(
            status="failed",
            summary="OpenCode is disabled (OPENCODE_ENABLED=0).",
            error="disabled",
            backend_used="opencode",
        )

    client = get_opencode_client()
    task_id = task.get("id") or task.get("task_id", "unknown")
    tenant_id = task.get("tenant_id", worker.tenant_id)
    description = task.get("description") or task.get("title") or ""

    # Build full prompt: task description + injected context
    prompt = description
    if context:
        prompt = f"{description}\n\n---\nContext from memory and prior tasks:\n{context}"

    session = await client.new_session(
        task_id=task_id,
        tenant_id=tenant_id,
        provider=OPENCODE_PROVIDER_CODING,
        model=OPENCODE_MODEL_CODING,
        workspace=str(worker.workspace),
    )

    chunks = []
    try:
        async for chunk in client.send_message(session, prompt):
            chunks.append(chunk)
            await worker.send_heartbeat(task_id, 50, "opencode-streaming")
    except Exception as e:
        _log(f"[{task_id}] OpenCode streaming error: {e}")

    oc_result = await client.close_session(session)
    status = "done" if oc_result.success else "failed"

    return _ExecResult(
        status=status,
        summary=oc_result.summary or "".join(chunks)[:2000],
        error=oc_result.error,
        backend_used=oc_result.backend_used,
        files_modified=oc_result.files_modified,
        raw_output=oc_result.raw_output,
    )


async def _run_task(worker: AgentWorker, runner, task: dict) -> None:
    task_id = task.get("id") or task.get("task_id", "unknown")
    _log(f"[{task_id}] starting execution")
    try:
        await worker.api("PATCH", f"/tasks/{task_id}/status", {"status": "in-progress"})
        await worker.execute_pending_sandboxes(task_id)
        context = await worker.build_preflight_context(task_id, task)
        await worker.send_heartbeat(task_id, 10, "started")

        # Ensure project/tenant scope is present for the runner
        if "project_id" not in task: task["project_id"] = worker.project_id
        if "tenant_id" not in task: task["tenant_id"] = worker.tenant_id

        # Route to OpenCode if the assigned agent uses opencode backend
        agent_name = task.get("agent") or task.get("agent_id") or ""
        from services.agents_config import get_preferred_backend
        _backend = get_preferred_backend(agent_name) if agent_name else "anthropic"
        if _backend == "opencode":
            _log(f"[{task_id}] routing to OpenCode backend (agent={agent_name})")
            result = await _run_opencode_task(worker, task, context)
        else:
            result = await asyncio.to_thread(runner.run, task_id, task, context)

        await worker.perform_post_task_actions(runner, task, result)
        completion_status = "done" if result.status in ("done", "partial") else "failed"
        await worker.report_completion(
            task_id,
            completion_status,
            result.summary,
            result.files_modified,
            getattr(result, "backend_used", "unknown"),
        )
        await worker.send_heartbeat(task_id, 100, "completed")
    except Exception as exc:
        failure = _classify_execution_error(exc, step="run_task")
        _log(
            f"[{task_id}] execution failed category={failure['category']} "
            f"retryable={failure['retryable']} message={failure['message']}"
        )
        await worker.report_completion(task_id, "failed", failure["message"], [], failure["category"])

# ── Sandbox ──────────────────────────────────────────────────────────────────
_SANDBOX_IMAGE = env_get("SANDBOX_IMAGE", default="python:3.12-slim")

def _validated_wdir(wdir: str) -> str:
    try:
        resolved = Path(wdir).resolve()
        resolved.relative_to(WORKSPACE.resolve())
        return str(resolved)
    except ValueError:
        raise ValueError(f"sandbox wdir {wdir!r} is outside WORKSPACE")

async def _docker_execute(script: str, wdir: str, timeout: int) -> tuple[str, str, int]:
    import tempfile
    safe_wdir = _validated_wdir(wdir)
    workspace_str = str(WORKSPACE.resolve())
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, dir=workspace_str) as tf:
        tf.write("#!/bin/bash\nset -euo pipefail\n" + script)
        script_host_path = tf.name
    rel_script = Path(script_host_path).relative_to(workspace_str)
    script_container_path = f"/workspace/{rel_script}"
    rel_wdir = Path(safe_wdir).relative_to(workspace_str)
    container_wdir = f"/workspace/{rel_wdir}" if str(rel_wdir) != "." else "/workspace"
    try:
        def _run():
            client = _docker_sdk.from_env(timeout=5)
            raw = client.containers.run(
                image=_SANDBOX_IMAGE, command=["bash", script_container_path],
                volumes={workspace_str: {"bind": "/workspace", "mode": "rw"}},
                working_dir=container_wdir, network_mode="none", read_only=True,
                mem_limit="256m", remove=True, stdout=True, stderr=True, detach=False,
            )
            return "passed", (raw.decode("utf-8", errors="replace"))[:8000], 0
        return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout)
    except Exception as e:
        return "failed", str(e), -1
    finally:
        try: os.remove(script_host_path)
        except OSError: pass

async def _host_execute(script: str, wdir: str, timeout: int) -> tuple[str, str, int]:
    safe_wdir = _validated_wdir(wdir)
    import tempfile, shutil
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tf:
        tf.write("#!/bin/bash\nset -euo pipefail\n" + script)
        script_path = tf.name
    os.chmod(script_path, 0o700)
    shell_path = shutil.which("bash") or shutil.which("sh") or shutil.which("bash.exe")
    if not shell_path: return "failed", "No shell found", -1
    try:
        proc = await asyncio.create_subprocess_exec(
            shell_path, script_path, cwd=safe_wdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode or 0
        output = (stdout + stderr).decode("utf-8", errors="replace")[:8000]
        return ("passed" if rc == 0 else "failed"), output, rc
    except Exception as e:
        return "failed", str(e), -1
    finally:
        try: os.remove(script_path)
        except OSError: pass

async def _safe_execute(script: str, wdir: str, timeout: int = 120) -> tuple[str, str, int]:
    try:
        safe_wdir = _validated_wdir(wdir)
    except ValueError as exc:
        return "failed", str(exc), 1

    if _HAS_DOCKER:
        status, output, rc = await _docker_execute(script, safe_wdir, timeout)
        if status == "passed":
            return status, output, rc
        if _ALLOW_HOST_SANDBOX_FALLBACK:
            _log("  docker sandbox failed; falling back to host execution by explicit opt-in")
            return await _host_execute(script, safe_wdir, timeout)
        return "failed", f"docker sandbox failed and host fallback is disabled: {output}", rc

    if _ALLOW_HOST_SANDBOX_FALLBACK:
        return await _host_execute(script, safe_wdir, timeout)
    return "failed", "docker sandbox unavailable and host fallback is disabled", -1

# ── Execution Loops ──────────────────────────────────────────────────────────
_TASK_COUNTER = 0


def _classify_execution_error(exc: Exception, *, step: str = "execution") -> dict[str, Any]:
    message = str(exc or "").strip()
    normalized = message.lower()
    category = "runtime"
    retryable = False

    if any(token in normalized for token in ["http 401", "http 403", "forbidden", "unauthorized", "api "]):
        category = "orchestrator_api"
    elif any(token in normalized for token in ["sandbox", "docker sandbox", "host fallback", "no shell found"]):
        category = "sandbox"
        retryable = "timeout" in normalized or "timed out" in normalized
    elif any(token in normalized for token in ["anthropic", "ollama", "rate limit", "model", "backend"]):
        category = "backend"
        retryable = True
    elif any(token in normalized for token in ["context", "reactivation", "entropy_risk", "memory_search", "lessons"]):
        category = "context"
    elif any(token in normalized for token in ["timeout", "timed out", "connection refused", "connection reset", "temporarily unavailable"]):
        category = "infrastructure"
        retryable = True
    elif any(token in normalized for token in ["validation", "assert", "test failed", "lint"]):
        category = "validation"

    return {
        "category": category,
        "step": step,
        "retryable": retryable,
        "message": message or repr(exc),
    }

async def _main_loop(worker, runner, ray_ok, ray_concurrency):
    _ray_pending = []
    from services.local_agent_runner import get_available_backends
    _log(f"Worker {worker.name} Loop Started (Service-Only Mode)")
    while True:
        try:
            runner.available_backends = await asyncio.to_thread(get_available_backends)
            
            # 1. Handle HTTP Dispatches
            task_resp = await worker.api("GET", f"/agents/{worker.name}/pending?limit=1")
            tasks = task_resp.get("tasks") or task_resp.get("pending") or []
            if tasks:
                await worker.run_task(runner, tasks[0])
                global _TASK_COUNTER
                _TASK_COUNTER += 1

            # 2. Memory Auditor Trigger
            if MEMORY_AUDITOR_ENABLED and MEMORY_AUDITOR_EVERY_N_TASKS > 0 and _TASK_COUNTER % MEMORY_AUDITOR_EVERY_N_TASKS == 0:
                subprocess.Popen([sys.executable, "services/memory_auditor.py"], cwd=str(Path(__file__).parent.parent))

        except Exception as e: _log(f"Cycle error: {e}")
        
        if EVENT_PUSH_ENABLED:
            try: await asyncio.wait_for(_task_event.wait(), timeout=float(POLL_INTERVAL))
            except asyncio.TimeoutError: pass
            _task_event.clear()
        else: await asyncio.sleep(POLL_INTERVAL)

async def main_async():
    sys.path.insert(0, str(Path(__file__).parent))
    try: from services.local_agent_runner import HybridAgentRunner
    except ImportError as e:
        _log(f"Import error: {e}")
        sys.exit(1)
    runner = HybridAgentRunner()
    worker = AgentWorker(AGENT_NAME, TENANT_ID)
    await worker.initialize()
    
    ray_ok = _ray_init()
    ray_concurrency = int(env_get("RAY_MAX_CONCURRENT", default="3"))
    if EVENT_PUSH_ENABLED:
        _log("event_push_requested - falling back to polling loop")
    await _main_loop(worker, runner, ray_ok, ray_concurrency)

def main():
    try:
        import asyncio
        asyncio.run(main_async())
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
