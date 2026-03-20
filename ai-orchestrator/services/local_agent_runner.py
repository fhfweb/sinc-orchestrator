from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from services.http_client import create_sync_resilient_client
from services.semantic_backend import (
    embed_text as _semantic_embed_text,
    search_points as _search_qdrant,
    upsert_point as _upsert_qdrant,
)
from services.streaming.core.config import env_get
from services.streaming.core import redis_ as redis_runtime

WORKSPACE = Path(__file__).resolve().parents[2]
OLLAMA_HOST = env_get("OLLAMA_HOST", default="http://ollama:11434").rstrip("/")
ANTHROPIC_API_KEY = env_get("ANTHROPIC_API_KEY", default="")
ANTHROPIC_MODEL = env_get("ANTHROPIC_MODEL", default="claude-3-5-sonnet-latest")
OLLAMA_MODEL = env_get("OLLAMA_MODEL", default="qwen2.5-coder:14b")
EMBEDDING_CACHE: dict[str, list[float]] = {}


@dataclass
class ExecutionResult:
    status: str
    summary: str
    error: str = ""
    backend_used: str = ""
    files_modified: list[str] = field(default_factory=list)
    raw_output: str = ""


class PlaywrightManager:
    _instance: "PlaywrightManager | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None

    @classmethod
    def get_instance(cls) -> "PlaywrightManager":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def get_browser(self):
        if self._browser is not None:
            return self._browser
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        browser_type = env_get("PLAYWRIGHT_BROWSER", default="chromium").lower()
        launcher = getattr(self._playwright, browser_type, self._playwright.chromium)
        self._browser = launcher.launch(headless=True)
        return self._browser

    @contextmanager
    def session(self, *, url: str | None = None):
        browser = self.get_browser()
        context = browser.new_context()
        page = context.new_page()
        try:
            if url:
                page.goto(url, wait_until="domcontentloaded")
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass


browser_manager = PlaywrightManager.get_instance()


def detect_codex() -> bool:
    return bool(env_get("CODEX_MODEL", default=""))


def detect_anthropic() -> bool:
    return bool(ANTHROPIC_API_KEY)


def detect_ollama() -> bool:
    return bool(OLLAMA_HOST)


def get_available_backends() -> list[str]:
    backends: list[str] = []
    if detect_codex():
        backends.append("codex")
    if detect_anthropic():
        backends.append("anthropic")
    if detect_ollama():
        backends.append("ollama")
    return backends or ["ollama"]


def detect_backends() -> list[str]:
    return get_available_backends()


def _resolve_path(path: str, workspace: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (workspace / candidate).resolve()
    return candidate


def _sync_http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any = None,
    timeout: int = 20,
    service_name: str = "local-agent-runner",
):
    with create_sync_resilient_client(service_name=service_name, timeout=timeout) as client:
        response = client.request(method, url, headers=headers, json=body)
        response.raise_for_status()
        return response


def _orchestrator_json_request(
    path: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: int = 20,
) -> dict[str, Any]:
    base_url = env_get("ORCHESTRATOR_URL", default="").rstrip("/")
    if not base_url:
        raise RuntimeError("ORCHESTRATOR_URL is not configured")
    from services import cognitive_orchestrator as cog

    ctx = cog.get_context()
    tenant_id = str(ctx.tenant_id or env_get("TENANT_ID", default="")).strip()
    if not tenant_id:
        raise cog.MissingTenantError("Explicit tenant_id is required for orchestrator requests")
    trace_id = str(ctx.trace_id or uuid.uuid4().hex[:12]).strip() or uuid.uuid4().hex[:12]
    project_id = str(ctx.project_id or env_get("PROJECT_ID", default="")).strip()
    headers = {
        "X-Tenant-Id": tenant_id,
        "X-Trace-Id": trace_id,
        "X-Correlation-ID": trace_id,
    }
    if project_id:
        headers["X-Project-Id"] = project_id
    response = _sync_http_request(
        f"{base_url}{path}",
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        service_name="local-agent-runner-orchestrator",
    )
    if hasattr(response, "json"):
        payload = response.json()
        return payload if isinstance(payload, dict) else {"result": payload}
    return {}


def _embed_text(text: str, model: str | None = None) -> tuple[list[float], str]:
    key = hashlib.sha1(f"{model or 'default'}:{text}".encode("utf-8")).hexdigest()
    cached = EMBEDDING_CACHE.get(key)
    if cached is not None:
        return cached, ""
    vector, error = _semantic_embed_text(text, model=model)
    if error:
        return [], error
    EMBEDDING_CACHE[key] = vector
    return vector, ""


def _memory_collection() -> str:
    return env_get("AGENT_MEMORY_COLLECTION", default="agent_memory")


def _language_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".php": "php",
        ".md": "markdown",
    }.get(suffix, suffix.lstrip(".") or "text")


def _python_analysis(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    functions: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    dependencies: set[str] = set()
    possible_bugs: list[str] = []
    max_complexity = 0
    docs: dict[str, str] = {}
    complexity_nodes = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.Match)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            dependencies.add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_complexity = 1 + sum(1 for child in ast.walk(node) if isinstance(child, complexity_nodes))
            max_complexity = max(max_complexity, body_complexity)
            functions.append(
                {
                    "name": node.name,
                    "lineno": getattr(node, "lineno", 0),
                    "args": [arg.arg for arg in node.args.args],
                    "complexity": body_complexity,
                }
            )
            doc = ast.get_docstring(node)
            if doc:
                docs[node.name] = doc
        elif isinstance(node, ast.ClassDef):
            classes.append({"name": node.name, "lineno": getattr(node, "lineno", 0)})
        elif isinstance(node, ast.Call):
            func_name = None
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            if func_name in {"print", "breakpoint", "pdb"}:
                possible_bugs.append(f"debug call detected: {func_name}")

    return {
        "language": "python",
        "functions": functions,
        "classes": classes,
        "dependencies": sorted(dependencies),
        "complexity": {"max_function_complexity": max_complexity},
        "possible_bugs": possible_bugs,
        "docstrings": docs,
    }


def _generic_analysis(source: str, language: str) -> dict[str, Any]:
    functions = [
        {"name": name, "lineno": idx + 1, "args": []}
        for idx, line in enumerate(source.splitlines())
        for name in re.findall(r"(?:function|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
    ]
    return {
        "language": language,
        "functions": functions,
        "classes": [],
        "dependencies": [],
        "complexity": {"max_function_complexity": 1},
        "possible_bugs": [],
        "docstrings": {},
    }


def _analyze_path(path: Path, mode: str = "full") -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    language = _language_for_path(path)
    data = _python_analysis(source) if language == "python" else _generic_analysis(source, language)
    if mode == "functions":
        return {"language": data["language"], "functions": data["functions"]}
    if mode == "dependencies":
        return {"language": data["language"], "dependencies": data["dependencies"]}
    if mode == "complexity":
        return {"language": data["language"], "complexity": data["complexity"]}
    return data


def _explain_code(path: Path, function_name: str | None = None) -> str:
    source = path.read_text(encoding="utf-8")
    language = _language_for_path(path)
    if language == "python":
        analysis = _python_analysis(source)
        if function_name:
            doc = analysis.get("docstrings", {}).get(function_name, "")
            summary = doc or f"Function {function_name} is defined in {path.name}."
            return f"{function_name}: {summary}"
        names = ", ".join(fn["name"] for fn in analysis["functions"][:5]) or "no functions detected"
        return f"{path.name} defines {names}."
    return f"{path.name} contains {language} code."


def _run_coro_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    holder: dict[str, Any] = {}

    def _runner():
        try:
            holder["result"] = asyncio.run(coro)
        except Exception as exc:
            holder["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


def _static_plan_fallback(goal: str, context: str = "") -> dict[str, Any]:
    tasks = [
        {"title": "Map current behavior", "depends_on": []},
        {"title": "Implement targeted change", "depends_on": ["Map current behavior"]},
        {"title": "Validate affected paths", "depends_on": ["Implement targeted change"]},
        {"title": "Capture follow-up risks", "depends_on": ["Validate affected paths"]},
    ]
    return {"ok": True, "source": "local_fallback", "goal": goal, "context": context, "tasks": tasks}


def _local_plan_tasks(goal: str, context: str = "") -> dict[str, Any]:
    return _static_plan_fallback(goal, context)


def _extract_paths_from_text(*parts: str) -> list[str]:
    found: list[str] = []
    for text in parts:
        for match in re.findall(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+", text or ""):
            if match not in found:
                found.append(match)
    return found


def _infer_incident_family(*parts: str) -> str:
    text = " ".join(part or "" for part in parts).lower()
    if any(token in text for token in ("security", "auth", "forbidden", "permission", "credential", "token")):
        return "security"
    if any(token in text for token in ("validation", "test", "assert", "schema", "422")):
        return "validation"
    if any(token in text for token in ("latency", "timeout", "slow", "performance")):
        return "performance"
    return "generic"


def _reflect(goal: str, action_taken: str, result: str, status: str) -> dict[str, Any]:
    text = " ".join([goal, action_taken, result]).lower()
    concerns: list[str] = []
    validation_status = "validated"
    if not any(token in text for token in ("test", "pytest", "validated", "validation", "lint", "smoke")):
        concerns.append("Validation evidence is missing.")
        validation_status = "missing_validation"
    if status != "done":
        concerns.append("Execution did not complete cleanly.")
    incident_family = _infer_incident_family(goal, action_taken, result)
    verdict = "good" if status == "done" and not concerns else "partial"
    memory_candidate = {
        "should_persist": True,
        "summary": (result or action_taken or goal)[:240],
        "tags": [incident_family, validation_status],
    }
    return {
        "verdict": verdict,
        "concerns": concerns,
        "next_steps": ["run validation", "capture diff"] if validation_status == "missing_validation" else ["persist learning"],
        "validation_status": validation_status,
        "incident_family": incident_family,
        "evidence": {
            "goal": goal,
            "action_taken": action_taken,
            "result": result,
            "status": status,
        },
        "memory_candidate": memory_candidate,
    }


def _incident_family_from_context(task: dict[str, Any], context: str) -> str:
    return _infer_incident_family(task.get("task_type", ""), task.get("description", ""), context)


def _risk_profile_from_context(context: str, incident_family: str) -> tuple[str, int]:
    match = re.search(r"blast_radius\s*=\s*(\d+)", context or "", re.IGNORECASE)
    blast_radius = int(match.group(1)) if match else 1
    profile = "normal"
    if incident_family == "security" or blast_radius >= 5:
        profile = "extreme"
    elif blast_radius >= 4 or "guarded" in (context or "").lower():
        profile = "guarded"
    elif blast_radius >= 2 or "elevated" in (context or "").lower():
        profile = "elevated"
    return profile, blast_radius


def _derive_autonomy_policy(task: dict[str, Any], context: str, backend: str) -> dict[str, Any]:
    task_type = str(task.get("task_type") or "generic").lower()
    if "frontend" in task_type:
        task_family = "frontend"
    elif "review" in task_type:
        task_family = "review"
    elif "incident" in task_type:
        task_family = "incident"
    elif "refactor" in task_type:
        task_family = "refactor"
    else:
        task_family = "backend"
    incident_family = _incident_family_from_context(task, context)
    risk_profile, blast_radius = _risk_profile_from_context(context, incident_family)
    requires_parallel_review = risk_profile in {"guarded", "extreme"} or task_family in {"review", "incident"}
    return {
        "task_family": task_family,
        "incident_family": incident_family,
        "risk_profile": risk_profile,
        "blast_radius": blast_radius,
        "requires_validation": task_family in {"backend", "incident", "refactor"} or incident_family != "generic",
        "requires_diff": blast_radius >= 3 or task_family in {"incident", "refactor"},
        "requires_parallel_review": requires_parallel_review,
        "backend": backend,
        "preferred_reviewers": ["code review agent", "qa agent"] if requires_parallel_review else [],
    }


def _finalize_task_complete(
    dispatch: dict[str, Any],
    completion: dict[str, Any],
    modified_files: list[str],
    tool_history: list[str],
) -> ExecutionResult:
    status = str(completion.get("status") or "partial")
    summary = str(completion.get("summary") or "")
    policy = dispatch.get("_autonomy_policy") or {}
    evidence = set(tool_history)
    missing: list[str] = []
    downgraded: list[str] = []

    if modified_files and not any(tool in evidence for tool in ("run_tests", "bash_exec")):
        missing.append("no_validation_evidence")
    if policy.get("requires_diff") and "diff_files" not in evidence:
        missing.append("missing_diff_review")
    if policy.get("requires_parallel_review") and "spawn_agent" not in evidence:
        missing.append("missing_parallel_review")
    if "self_reflect" not in evidence:
        missing.append("missing_structured_reflection")
    if policy.get("incident_family") == "security" and "spawn_agent" not in evidence:
        downgraded.append("critical_incident_requires_review")
    if int(policy.get("blast_radius") or 0) >= 4 and "diff_files" not in evidence:
        downgraded.append("missing_diff_for_blast_radius")
    if status == "done" and missing:
        status = "partial"
        if "no_validation_evidence" in missing:
            downgraded.insert(0, "no_validation_evidence")

    suffix = []
    suffix.extend(missing)
    suffix.extend(f"downgraded_to_partial:{item}" for item in downgraded)
    if suffix:
        summary = "; ".join(filter(None, [summary] + suffix))
    return ExecutionResult(
        status=status,
        summary=summary,
        error=str(completion.get("error") or ""),
        backend_used=str(policy.get("backend") or completion.get("backend_used") or ""),
        files_modified=modified_files or list(completion.get("files_modified") or []),
        raw_output=json.dumps(completion),
    )


def _health_component_status(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("status") or value.get("health") or "unknown")
    return str(value or "unknown")


def _parse_log_lines(lines: list[str]) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    totals = {"ERROR": 0, "WARN": 0, "INFO": 0}
    grouped: dict[str, dict[str, Any]] = {}
    for line in lines:
        level = "INFO"
        if "ERROR" in line:
            level = "ERROR"
        elif "WARN" in line or "WARNING" in line:
            level = "WARN"
        totals[level] = totals.get(level, 0) + 1
        normalized = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        bucket = grouped.setdefault(normalized, {"fingerprint": hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12], "message": normalized, "count": 0, "level": level})
        bucket["count"] += 1
        if level == "ERROR":
            bucket["level"] = "ERROR"
    patterns = sorted(grouped.values(), key=lambda item: (-item["count"], item["message"]))
    anomalies = [item for item in patterns if item["count"] > 1 or item["level"] == "ERROR"]
    return totals, patterns, anomalies


def _wait_for_tasks_via_stream(
    *,
    task_ids: list[str],
    timeout_s: float,
    poll_interval_s: float,
    tenant_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    redis = redis_runtime.get_redis()
    stream_name = f"sinc:stream:task_lifecycle:{tenant_id}"
    snapshots: dict[str, Any] = {}
    completed: dict[str, dict[str, Any]] = {}
    if redis is None:
        return completed, {"available": False, "mode": "polling", "stream_name": stream_name, "snapshots": snapshots}

    last_id = "$"
    start = time.monotonic()
    terminal = {"done", "failed", "cancelled", "partial", "completed"}
    while time.monotonic() - start <= max(timeout_s, poll_interval_s, 0.01):
        reads = redis.xread({stream_name: last_id}, block=max(int(poll_interval_s * 1000), 1), count=20) or []
        for _stream, entries in reads:
            for entry_id, fields in entries:
                last_id = entry_id
                raw = fields.get("data") if isinstance(fields, dict) else None
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                task_id = str(payload.get("task_id") or "")
                if task_id not in task_ids:
                    continue
                snapshots[task_id] = payload
                if str(payload.get("status") or "").lower() in terminal:
                    completed[task_id] = {
                        "task_id": task_id,
                        "status": payload.get("status"),
                        "summary": payload.get("summary", ""),
                        "assigned_agent": payload.get("agent_name") or payload.get("assigned_agent", ""),
                        "result": payload,
                    }
        if len(completed) >= len(task_ids):
            break
    return completed, {"available": True, "mode": "redis-stream", "stream_name": stream_name, "snapshots": snapshots}


def _build_tools() -> list[dict[str, Any]]:
    names = [
        "read_file",
        "write_file",
        "patch_file",
        "bash_exec",
        "semantic_search",
        "analyze_code",
        "explain_code",
        "plan_tasks",
        "memory_search",
        "memory_write",
        "self_reflect",
        "spawn_agent",
        "root_cause_analysis",
        "analyze_logs",
        "system_health_check",
        "diff_files",
        "task_complete",
    ]
    return [{"name": name, "description": name.replace("_", " ")} for name in names]


def _build_anthropic_tools() -> list[dict[str, Any]]:
    return _build_tools()


def _build_ollama_tools() -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": tool["name"], "description": tool["description"]}} for tool in _build_tools()]


def _semantic_search_local(query: str, workspace: Path, top_k: int = 5) -> list[dict[str, Any]]:
    lowered = query.lower()
    results: list[dict[str, Any]] = []
    for path in workspace.rglob("*"):
        if not path.is_file() or path.stat().st_size > 200_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if lowered in text.lower():
            idx = text.lower().find(lowered)
            snippet = text[max(0, idx - 80): idx + 160].replace("\n", " ")
            results.append({"path": str(path.relative_to(workspace)), "score": 0.9, "content": snippet.strip()})
        if len(results) >= top_k:
            break
    return results


def _execute_tool(name: str, payload: dict[str, Any], workspace: Path) -> str:
    workspace = Path(workspace)

    if name == "read_file":
        path = _resolve_path(str(payload["path"]), workspace)
        return path.read_text(encoding="utf-8")

    if name == "write_file":
        path = _resolve_path(str(payload["path"]), workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(payload.get("content") or ""), encoding="utf-8")
        return f"OK: wrote {path.name}"

    if name == "patch_file":
        path = _resolve_path(str(payload["path"]), workspace)
        source = path.read_text(encoding="utf-8") if path.exists() else ""
        old = str(payload.get("old_str") or "")
        new = str(payload.get("new_str") or "")
        updated = source.replace(old, new, 1) if old else source + new
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
        return f"OK: patched {path.name}"

    if name == "bash_exec":
        command = str(payload.get("command") or "")
        proc = subprocess.run(command, cwd=str(workspace), shell=True, capture_output=True, text=True)
        output = proc.stdout or proc.stderr
        return json.dumps({"rc": proc.returncode, "output": output[:4000]})

    if name == "analyze_code":
        path = _resolve_path(str(payload["path"]), workspace)
        return json.dumps(_analyze_path(path, str(payload.get("mode") or "full")))

    if name == "explain_code":
        path = _resolve_path(str(payload["path"]), workspace)
        return _explain_code(path, payload.get("function_name"))

    if name == "plan_tasks":
        goal = str(payload.get("goal") or "")
        context = str(payload.get("context") or "")
        return json.dumps(_local_plan_tasks(goal, context))

    if name == "semantic_search":
        query = str(payload.get("query") or payload.get("q") or "")
        top_k = int(payload.get("top_k") or 5)
        return json.dumps({"results": _semantic_search_local(query, workspace, top_k=top_k)})

    if name == "memory_search":
        query = str(payload.get("query") or "")
        top_k = int(payload.get("top_k") or 5)
        vector, error = _embed_text(query)
        if error:
            return json.dumps({"results": [], "error": error})
        rows, q_error = _search_qdrant(_memory_collection(), vector, top_k=top_k)
        if q_error:
            return json.dumps({"results": [], "error": q_error})
        results = []
        for row in rows:
            meta = dict(row.get("payload") or {})
            results.append(
                {
                    "score": row.get("score", 0),
                    "content": meta.get("content", ""),
                    "metadata": meta,
                }
            )
        return json.dumps({"results": results})

    if name == "memory_write":
        content = str(payload.get("content") or payload.get("summary") or "")
        vector, error = _embed_text(content)
        if error:
            return f"ERROR: {error}"
        metadata = dict(payload.get("metadata") or {})
        for field in ("task_id", "task_type", "agent_name", "incident_family", "risk_level"):
            if payload.get(field) is not None:
                metadata[field] = payload.get(field)
        if payload.get("files"):
            metadata["files"] = payload.get("files")
        record = {
            "key": payload.get("key") or str(uuid.uuid4()),
            "content": content,
            "tags": payload.get("tags") or [],
            "source": payload.get("source") or _memory_collection(),
            "verified": bool(payload.get("verified", False)),
            "metadata": metadata,
        }
        _upsert_qdrant(_memory_collection(), vector, record)
        return "OK: memory stored"

    if name == "self_reflect":
        return json.dumps(
            _reflect(
                str(payload.get("goal") or ""),
                str(payload.get("action_taken") or ""),
                str(payload.get("result") or ""),
                str(payload.get("status") or "partial"),
            )
        )

    if name == "spawn_agent":
        subtasks = list(payload.get("subtasks") or [])
        mode = str(payload.get("mode") or "single")
        if mode == "review_parallel" and not subtasks:
            subtasks = [
                {"title": payload.get("title") or "Review", "goal": payload.get("goal") or "", "agent_type": reviewer}
                for reviewer in list(payload.get("reviewers") or [])
            ]
        if not subtasks:
            subtasks = [{"title": payload.get("title") or "Subtask", "goal": payload.get("goal") or "", "agent_type": payload.get("agent_type") or payload.get("agent") or "ai engineer"}]

        created: list[dict[str, Any]] = []
        for item in subtasks:
            created_task = _orchestrator_json_request(
                "/tasks",
                method="POST",
                body={
                    "title": item.get("title"),
                    "description": item.get("goal") or item.get("description") or "",
                    "agent": item.get("agent_type") or item.get("agent") or "ai engineer",
                },
            )
            created.append(created_task)
        task_ids = [str(item.get("task_id") or item.get("id") or "") for item in created if item.get("task_id") or item.get("id")]
        result: dict[str, Any] = {
            "ok": True,
            "mode": mode,
            "fan_out": len(task_ids),
            "task_id": task_ids[0] if len(task_ids) == 1 else None,
            "task_ids": task_ids,
            "status": created[0].get("status") if len(created) == 1 and created else "pending",
        }
        wait = bool(payload.get("wait"))
        poll_interval_s = float(payload.get("poll_interval_s") or 1.0)
        timeout_s = float(payload.get("timeout_s") or 30.0)
        tenant_id = env_get("TENANT_ID", default="") or "tenant-test"
        if wait and task_ids:
            completed, lifecycle = _wait_for_tasks_via_stream(
                task_ids=task_ids,
                timeout_s=timeout_s,
                poll_interval_s=poll_interval_s,
                tenant_id=tenant_id,
            )
            pending_ids = [task_id for task_id in task_ids if task_id not in completed]
            timed_out = []
            if pending_ids:
                timed_out = list(pending_ids)
                if payload.get("cancel_on_timeout"):
                    for task_id in pending_ids:
                        _orchestrator_json_request(f"/tasks/{task_id}/status", method="PATCH", body={"status": "cancelled"})
            lifecycle = dict(lifecycle or {})
            lifecycle.setdefault("mode", "redis-stream" if lifecycle.get("available", True) else "polling")
            result["lifecycle"] = lifecycle
            result["fan_in"] = {"completed": len(completed), "timed_out": len(timed_out)}
            result["timed_out"] = timed_out
            result["results"] = list(completed.values())
            if len(task_ids) == 1 and completed:
                only = next(iter(completed.values()))
                result.update({
                    "task_id": only["task_id"],
                    "status": only["status"],
                    "summary": only.get("summary", ""),
                    "assigned_agent": only.get("assigned_agent", ""),
                })
            if mode == "review_parallel":
                approved = all(str(item.get("status") or "").lower() == "done" for item in completed.values()) and len(completed) == len(task_ids)
                result["consensus"] = {"approved": approved, "completed": len(completed), "expected": len(task_ids)}
        return json.dumps(result)

    if name == "analyze_logs":
        components = list(payload.get("components") or ([] if not payload.get("component") else [payload.get("component")])) or ["worker"]
        pattern = str(payload.get("pattern") or "")
        component_rows = []
        all_lines: list[str] = []
        for component in components:
            path = f"/api/v5/dashboard/diagnostics/logs?component={component}"
            if pattern:
                path += f"&pattern={pattern}"
            response = _orchestrator_json_request(path)
            lines = list(response.get("lines") or [])
            totals, patterns, anomalies = _parse_log_lines(lines)
            component_rows.append({"component": component, "totals": totals, "patterns": patterns[:5], "anomalies": anomalies[:5]})
            all_lines.extend(lines)
        totals, patterns, anomalies = _parse_log_lines(all_lines)
        recommendations = []
        if anomalies:
            recommendations.append("Investigate recurring errors before widening fan-out.")
        return json.dumps({"components": component_rows, "totals": totals, "patterns": patterns[:10], "anomalies": anomalies[:10], "recommendations": recommendations})

    if name == "root_cause_analysis":
        task_id = str(payload.get("task_id") or "")
        task = _orchestrator_json_request(f"/tasks/{task_id}")
        debugger = _orchestrator_json_request(f"/api/v5/dashboard/task-debugger/{task_id}")
        events = _orchestrator_json_request(f"/tasks/{task_id}/events")
        context = _orchestrator_json_request(f"/tasks/{task_id}/context")
        impact = _orchestrator_json_request(f"/tasks/{task_id}/impact")
        readiness = _orchestrator_json_request("/readiness/live")
        incidents = _orchestrator_json_request("/incidents?limit=5")
        logs = json.loads(_execute_tool("analyze_logs", {"components": ["worker", "orch"], "pattern": task_id}, workspace))
        text = json.dumps([task, debugger, events, context, impact, readiness, incidents, logs]).lower()
        primary_cause = "generic_failure"
        if "validation" in text or "tests failed" in text or "validator failed" in text:
            primary_cause = "validation_failure"
        recommendations = ["Inspect failing validator path", "Re-run targeted validation"] if primary_cause == "validation_failure" else ["Inspect task timeline"]
        return json.dumps({
            "task_id": task_id,
            "primary_cause": primary_cause,
            "recommendations": recommendations,
            "evidence": {
                "open_incidents": len(incidents.get("incidents") or []),
                "context_nodes": len(context.get("nodes") or []),
                "timeline_events": len(debugger.get("timeline") or []),
                "blast_radius": impact.get("blast_radius"),
            },
            "debugger": debugger,
            "logs": logs,
        })

    if name == "system_health_check":
        readiness = _orchestrator_json_request("/readiness/live")
        summary = _orchestrator_json_request("/api/v5/dashboard/summary")
        diagnostics = _orchestrator_json_request("/api/v5/dashboard/diagnostics/health")
        deep = _orchestrator_json_request("/health/deep")
        incidents = _orchestrator_json_request("/incidents?limit=5")
        logs = json.loads(_execute_tool("analyze_logs", {"components": ["worker", "orch"]}, workspace))
        issues = []
        for name_, value in dict(diagnostics.get("components") or {}).items():
            status = _health_component_status(value)
            if status not in {"up", "ok", "healthy", "available"}:
                issues.append(f"{name_}={status}")
        status = "ok"
        if readiness.get("health") not in {"ok", "ready"} or issues or incidents.get("incidents"):
            status = "degraded"
        return json.dumps({
            "status": status,
            "issues": issues,
            "open_incidents": len(incidents.get("incidents") or []) or int(readiness.get("open_incidents") or 0),
            "summary": summary,
            "deep": deep,
            "logs": logs,
        })

    if name == "task_complete":
        completion = dict(payload)
        dispatch = dict(payload.get("dispatch") or {})
        modified = list(payload.get("files_modified") or completion.get("files_modified") or [])
        history = list(payload.get("tool_history") or [])
        result = _finalize_task_complete(dispatch, completion, modified, history)
        return json.dumps(result.__dict__)

    if name == "diff_files":
        before = _resolve_path(str(payload.get("before") or payload.get("path_before") or ""), workspace)
        after = _resolve_path(str(payload.get("after") or payload.get("path_after") or ""), workspace)
        return json.dumps({"before": str(before), "after": str(after)})

    raise ValueError(f"unknown tool: {name}")


def run_codex(prompt: str, *, workspace: Path = WORKSPACE, dispatch: dict[str, Any] | None = None) -> ExecutionResult:
    return ExecutionResult(status="partial", summary="Codex backend not wired in this environment.", backend_used="codex")


def run_anthropic(prompt: str, *, workspace: Path = WORKSPACE, dispatch: dict[str, Any] | None = None) -> ExecutionResult:
    return ExecutionResult(status="partial", summary="Anthropic backend unavailable in local runner fallback.", backend_used="anthropic")


def run_ollama(prompt: str, *, workspace: Path = WORKSPACE, dispatch: dict[str, Any] | None = None) -> ExecutionResult:
    return ExecutionResult(status="partial", summary="Ollama backend unavailable in local runner fallback.", backend_used="ollama")


class HybridAgentRunner:
    def __init__(self, available_backends: list[str] | None = None):
        self.available_backends = available_backends or get_available_backends()

    def _build_autonomy_brief(self, task_id: str, task: dict[str, Any], preflight_context: str) -> str:
        description = str(task.get("description") or task.get("title") or "")
        files = list(task.get("files_affected") or _extract_paths_from_text(description, preflight_context))
        backend = self.available_backends[0] if self.available_backends else "ollama"
        policy = _derive_autonomy_policy(task, preflight_context, backend)

        plan = json.loads(_execute_tool("plan_tasks", {"goal": description or task_id, "context": preflight_context}, WORKSPACE))
        memory = json.loads(_execute_tool("memory_search", {"query": description or task_id, "top_k": 3}, WORKSPACE))
        semantic = json.loads(_execute_tool("semantic_search", {"query": description or task_id, "top_k": 3}, WORKSPACE))
        structural = {}
        if files:
            structural = json.loads(_execute_tool("analyze_code", {"path": files[0], "mode": "full"}, WORKSPACE))

        lines = [
            f"AUTONOMY DOSSIER FOR {task_id}",
            "ADAPTIVE POLICY:",
            json.dumps(policy, ensure_ascii=True),
            "EXECUTION PLAN:",
        ]
        lines.extend(f"- {item['title']}" for item in plan.get("tasks", []))
        lines.append("RELEVANT MEMORY:")
        lines.extend(f"- {item.get('content', '')}" for item in memory.get("results", []))
        lines.append("SEMANTIC HITS:")
        lines.extend(f"- {item.get('content', '')}" for item in semantic.get("results", []))
        lines.append("STRUCTURAL RISKS:")
        for issue in structural.get("possible_bugs", []):
            lines.append(f"- {issue}")
        if not structural.get("possible_bugs"):
            lines.append("- no structural red flags detected")
        return "\n".join(lines)

    def run(self, prompt: str, task: dict[str, Any] | None = None, workspace: Path | None = None) -> ExecutionResult:
        workspace = Path(workspace or WORKSPACE)
        dispatch = dict(task or {})
        backend = self.available_backends[0] if self.available_backends else "ollama"
        brief = ""
        if dispatch:
            try:
                brief = self._build_autonomy_brief(str(dispatch.get("id") or dispatch.get("title") or "task"), dispatch, prompt)
                dispatch["_autonomy_policy"] = _derive_autonomy_policy(dispatch, brief, backend)
            except Exception as exc:
                brief = f"AUTONOMY DOSSIER UNAVAILABLE: {exc}"
        final_prompt = f"{brief}\n\n{prompt}" if brief else prompt
        if backend == "codex":
            return run_codex(final_prompt, workspace=workspace, dispatch=dispatch)
        if backend == "anthropic":
            return run_anthropic(final_prompt, workspace=workspace, dispatch=dispatch)
        return run_ollama(final_prompt, workspace=workspace, dispatch=dispatch)
