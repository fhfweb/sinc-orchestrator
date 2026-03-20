from __future__ import annotations

import json
import os
import subprocess
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _discover_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (
            (candidate / "docs" / "agents" / "agents-360.registry.json").exists()
            and (candidate / "ai-orchestrator").exists()
        ):
            return candidate
    for candidate in (start, *start.parents):
        if (candidate / "docs" / "agents" / "agents-360.registry.json").exists():
            return candidate
    return start.parents[4]


def _extract_last_json(raw: str) -> dict[str, Any] | None:
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


@dataclass
class CommandResult:
    success: bool
    output: str
    error: str
    returncode: int
    json: dict[str, Any] | None = None


class AgentWorker:
    """
    Bridge between Python native runtime and the existing PowerShell orchestrator API.
    """

    NATIVE_EXECUTION_MODES = {
        "native-agent",
        "llm-native",
        "python-runtime",
        "autonomous-native",
        "v4-native",
    }
    NATIVE_RUNTIME_ENGINES = {"python", "native", "hybrid", "llm-native", "v4-native"}

    def __init__(self, project_path: str, agent_name: str, python_executable: str = "python") -> None:
        self.project_path = Path(project_path).resolve()
        self.agent_name = (agent_name or "Codex").strip()
        self.python_executable = python_executable
        self.tool_timeout_seconds = int(os.getenv("ORCHESTRATOR_TOOL_TIMEOUT_SECONDS", "120"))
        self.memory_timeout_seconds = int(os.getenv("ORCHESTRATOR_MEMORY_TIMEOUT_SECONDS", "20"))

        self.repo_root = _discover_repo_root(Path(__file__).resolve())
        self.orchestrator_root = self.project_path / "ai-orchestrator"
        self.legacy_v2_root = self.repo_root / "ai-orchestrator" / "scripts" / "v2"

        self.universal_ps1 = self.legacy_v2_root / "Invoke-UniversalOrchestratorV2.ps1"
        self.preflight_ps1 = self.legacy_v2_root / "Invoke-PreFlightReasoner.ps1"
        self.step_checkpoint_ps1 = self.legacy_v2_root / "Invoke-StepCheckpoint.ps1"
        self.validator_ps1 = self.legacy_v2_root / "Invoke-OutputSchemaValidator.ps1"
        self.dispatcher_ps1 = self.legacy_v2_root / "Invoke-AgentToolDispatcher.ps1"
        self.ephemeral_runner_ps1 = self.legacy_v2_root / "Invoke-EphemeralToolRunner.ps1"
        self.graph_query_py = self.repo_root / "scripts" / "graph_query.py"
        self.query_lessons_py = self.repo_root / "scripts" / "query_lessons.py"
        self.registry_path = self.repo_root / "docs" / "agents" / "agent-tools-registry.json"
        self.tmp_tools_dir = self.project_path / "workspace" / "tmp_tools"

    # ---------------------------
    # DAG / task helpers
    # ---------------------------
    def load_dag(self) -> dict[str, Any]:
        return _safe_read_json(self.orchestrator_root / "tasks" / "task-dag.json")

    def list_assigned_in_progress_tasks(self, max_tasks: int = 3) -> list[dict[str, Any]]:
        dag = self.load_dag()
        tasks = dag.get("tasks", [])
        selected: list[dict[str, Any]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            status = str(task.get("status", "")).strip().lower()
            assigned = str(task.get("assigned_agent", "")).strip()
            if status == "in-progress" and assigned == self.agent_name:
                selected.append(task)
            if len(selected) >= max_tasks:
                break
        return selected

    @staticmethod
    def is_native_task(task: dict[str, Any]) -> bool:
        execution_mode = str(task.get("execution_mode", "")).strip().lower()
        runtime_engine = str(task.get("runtime_engine", "")).strip().lower()
        if execution_mode in AgentWorker.NATIVE_EXECUTION_MODES:
            return True
        if runtime_engine in AgentWorker.NATIVE_RUNTIME_ENGINES:
            return True
        return False

    # ---------------------------
    # command bridges
    # ---------------------------
    def _run_powershell_file(self, script_path: Path, named_args: dict[str, Any] | None = None) -> CommandResult:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
        if named_args:
            for key, value in named_args.items():
                if value is None:
                    continue
                key_text = f"-{key}"
                if isinstance(value, bool):
                    if value:
                        cmd.append(key_text)
                    continue
                cmd.extend([key_text, str(value)])

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.tool_timeout_seconds,
            )
            output = proc.stdout or ""
            error = proc.stderr or ""
            parsed = _extract_last_json(output)
            return CommandResult(
                success=(proc.returncode == 0),
                output=output,
                error=error,
                returncode=proc.returncode,
                json=parsed,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                success=False,
                output=(exc.stdout or ""),
                error=f"tool-timeout:{self.tool_timeout_seconds}s",
                returncode=124,
                json=None,
            )

    def run_powershell_command(self, command: str) -> CommandResult:
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(self.project_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = proc.stdout or ""
        error = proc.stderr or ""
        return CommandResult(
            success=(proc.returncode == 0),
            output=output,
            error=error,
            returncode=proc.returncode,
        )

    # ---------------------------
    # Orchestrator APIs
    # ---------------------------
    def generate_preflight(self, task_id: str) -> CommandResult:
        return self._run_powershell_file(
            self.preflight_ps1,
            {
                "ProjectPath": str(self.project_path),
                "TaskId": task_id,
                "AgentName": self.agent_name,
                "EmitJson": True,
            },
        )

    def write_step_checkpoint(
        self,
        task_id: str,
        step_number: int,
        step_name: str,
        status: str,
        details: str = "",
        error_text: str = "",
    ) -> CommandResult:
        return self._run_powershell_file(
            self.step_checkpoint_ps1,
            {
                "Mode": "write",
                "ProjectPath": str(self.project_path),
                "TaskId": task_id,
                "StepNumber": step_number,
                "StepName": step_name,
                "Status": status,
                "AgentName": self.agent_name,
                "Details": details,
                "ErrorText": error_text,
            },
        )

    def clear_step_checkpoints(self, task_id: str) -> CommandResult:
        return self._run_powershell_file(
            self.step_checkpoint_ps1,
            {
                "Mode": "clear",
                "ProjectPath": str(self.project_path),
                "TaskId": task_id,
            },
        )

    def validate_completion_payload(self, payload_path: str) -> CommandResult:
        return self._run_powershell_file(
            self.validator_ps1,
            {
                "ProjectPath": str(self.project_path),
                "AgentName": self.agent_name,
                "PayloadPath": payload_path,
                "EmitJson": True,
            },
        )

    def complete_task(
        self,
        task_id: str,
        payload_path: str,
        notes: str,
        artifacts: list[str],
    ) -> CommandResult:
        artifacts_csv = ",".join([p for p in artifacts if p])
        return self._run_powershell_file(
            self.universal_ps1,
            {
                "Mode": "complete",
                "ProjectPath": str(self.project_path),
                "TaskId": task_id,
                "AgentName": self.agent_name,
                "CompletionPayloadPath": payload_path,
                "Notes": notes,
                "Artifacts": artifacts_csv,
            },
        )

    def dispatch_tool(self, tool_name: str, tool_args: dict[str, Any]) -> CommandResult:
        return self._run_powershell_file(
            self.dispatcher_ps1,
            {
                "ProjectPath": str(self.project_path),
                "AgentName": self.agent_name,
                "ToolName": tool_name,
                "ToolArgumentsJson": json.dumps(tool_args, ensure_ascii=False),
                "EmitJson": True,
            },
        )

    # ---------------------------
    # Memory / context
    # ---------------------------
    def query_lessons(self, query: str, top_k: int = 5) -> dict[str, Any]:
        if not self.query_lessons_py.exists():
            return {}
        cmd = [
            self.python_executable,
            str(self.query_lessons_py),
            "--project-path",
            str(self.project_path),
            "--query",
            query,
            "--top-k",
            str(top_k),
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.memory_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {}
        if proc.returncode != 0:
            return {}
        try:
            parsed = json.loads(proc.stdout or "{}")
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {}

    # ---------------------------
    # Completion payload helpers
    # ---------------------------
    def _derive_modules(self, files_written: list[str]) -> list[str]:
        modules: list[str] = []
        for path in files_written:
            normalized = path.replace("\\", "/").strip().lstrip("./")
            if not normalized:
                continue
            parts = [p for p in normalized.split("/") if p]
            if not parts:
                continue
            if "." in parts[-1]:
                parts = parts[:-1]
            if not parts:
                continue
            module = parts[0] if len(parts) == 1 else f"{parts[0]}/{parts[1]}"
            if module not in modules:
                modules.append(module)
        return modules

    def build_completion_payload(
        self,
        task: dict[str, Any],
        summary: str,
        files_written: list[str],
        tests_passed: bool,
        validation: list[str],
    ) -> dict[str, Any]:
        task_id = str(task.get("id", ""))
        cleaned_files = [f for f in files_written if isinstance(f, str) and f.strip()]
        if not cleaned_files:
            fallback_files = task.get("files_affected", [])
            if isinstance(fallback_files, list):
                cleaned_files = [str(f) for f in fallback_files if str(f).strip()]
        if not cleaned_files:
            cleaned_files = ["ai-orchestrator/tasks/execution-history.md"]

        source_modules = self._derive_modules(cleaned_files)
        payload = {
            "schema_version": "v2",
            "task_id": task_id,
            "agent_name": self.agent_name,
            "timestamp": _ts(),
            "summary": summary.strip() if summary else f"Task {task_id} completed by {self.agent_name}.",
            "files_written": cleaned_files,
            "tests_passed": bool(tests_passed),
            "validation": [v for v in validation if isinstance(v, str) and v.strip()],
            "source_files": cleaned_files,
            "source_modules": source_modules,
            "tool_calls": [],
            "risks": [],
            "next_steps": [],
            "local_library_candidates": [],
            "library_decision": {
                "selected_option": "not-applicable",
                "justification": "No local library reuse required for this task.",
                "selected_libraries": [],
                "rejected_libraries": [],
            },
        }
        if not payload["validation"]:
            payload["validation"] = ["runtime-native-loop"]
        return payload

    def save_completion_payload(self, task_id: str, payload: dict[str, Any]) -> str:
        target_dir = self.orchestrator_root / "tasks" / "completions"
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_task = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_id)
        file_name = f"{safe_task}-py-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json"
        path = target_dir / file_name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        try:
            return str(path.relative_to(self.project_path)).replace("\\", "/")
        except ValueError:
            return str(path)

    # ---------------------------
    # Ephemeral Tool Forging
    # ---------------------------
    def _compute_sha256(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _compute_tool_signature(
        self,
        tool_name: str,
        script_sha256: str,
        registered_at: str,
        expires_at: str,
    ) -> str:
        secret = os.getenv("ORCHESTRATOR_TOOL_SIGNING_KEY", "").strip()
        if not secret:
            return ""
        payload = f"{tool_name}|{script_sha256}|{registered_at}|{expires_at}"
        digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest

    def _is_ephemeral_tool_body_allowed(self, script_body: str) -> tuple[bool, str]:
        """
        Deny obvious dangerous primitives. This is intentionally strict because
        ephemeral tools are dynamic and must be constrained by default.
        """
        body = script_body or ""
        lowered = body.lower()
        deny_patterns = [
            r"\binvoke-expression\b",
            r"\biex\b",
            r"\bstart-process\b",
            r"\bremove-item\b",
            r"\bdel\b\s+",
            r"\brd\b\s+",
            r"\bset-content\b",
            r"\badd-content\b",
            r"\bnew-item\b",
            r"\bcopy-item\b",
            r"\bmove-item\b",
            r"\brename-item\b",
            r"\bstop-process\b",
            r"\binvoke-webrequest\b",
            r"\binvoke-restmethod\b",
            r"\bnew-object\s+system\.net\b",
            r"\[system\.io\.file\]::",
            r"\[system\.io\.directory\]::",
            r"\$env:",
        ]
        for pattern in deny_patterns:
            if re.search(pattern, lowered, re.IGNORECASE):
                return False, f"denylist-match:{pattern}"
        return True, ""

    def _lint_ephemeral_powershell_script(self, script_path: Path) -> tuple[bool, str]:
        """
        Parse-time lint with PowerShell parser. Registration fails when parser errors exist.
        """
        escaped = str(script_path).replace("'", "''")
        lint_cmd = (
            "$t=$null; $e=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped}', [ref]$t, [ref]$e) | Out-Null; "
            "if ($e -and $e.Count -gt 0) { "
            "$msgs = ($e | ForEach-Object { $_.Message }) -join '; '; "
            "Write-Output $msgs; exit 1 "
            "}"
        )
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            lint_cmd,
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.project_path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.tool_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return False, "lint-timeout"
        if proc.returncode != 0:
            return False, (proc.stdout or proc.stderr or "lint-failed").strip()
        return True, ""

    def register_ephemeral_tool(
        self,
        tool_name: str,
        description: str,
        script_body: str,
        parameters_schema: dict[str, Any] | None = None,
    ) -> str:
        """Write a new ephemeral tool (.ps1 + manifest .json) to workspace/tmp_tools/."""
        allowed_roles_raw = os.getenv(
            "ORCHESTRATOR_FORGE_ALLOWED_ROLES",
            "ai architect,claude code,claude",
        )
        allowed_roles = {
            r.strip().lower()
            for r in (allowed_roles_raw or "").split(",")
            if r and r.strip()
        }
        current_role = self.agent_name.strip().lower()
        if allowed_roles and current_role not in allowed_roles:
            return f"error: forge-role-not-allowed:{current_role}"

        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in tool_name.strip())
        if not safe_name:
            return "error: invalid tool_name"
        if not script_body or not script_body.strip():
            return "error: empty-script-body"

        allowed, deny_reason = self._is_ephemeral_tool_body_allowed(script_body)
        if not allowed:
            return f"error: governance-denylist:{deny_reason}"

        self.tmp_tools_dir.mkdir(parents=True, exist_ok=True)
        script_path = self.tmp_tools_dir / f"{safe_name}.ps1"
        manifest_path = self.tmp_tools_dir / f"{safe_name}.json"

        script_path.write_text(script_body, encoding="utf-8")
        lint_ok, lint_error = self._lint_ephemeral_powershell_script(script_path)
        if not lint_ok:
            try:
                script_path.unlink(missing_ok=True)
            except Exception:
                pass
            return f"error: lint-failed:{lint_error}"

        now = datetime.now(timezone.utc)
        ttl_hours = int(os.getenv("ORCHESTRATOR_EPHEMERAL_TOOL_TTL_HOURS", "24"))
        if ttl_hours < 1:
            ttl_hours = 1
        if ttl_hours > 168:
            ttl_hours = 168
        expires_at = (now + timedelta(hours=ttl_hours)).isoformat()
        script_sha256 = self._compute_sha256(script_body)
        signature = self._compute_tool_signature(
            tool_name=safe_name,
            script_sha256=script_sha256,
            registered_at=now.isoformat(),
            expires_at=expires_at,
        )

        manifest: dict[str, Any] = {
            "tool_name": safe_name,
            "description": description,
            "registered_by": self.agent_name,
            "registered_at": now.isoformat(),
            "expires_at": expires_at,
            "status": "active",
            "script_sha256": script_sha256,
            "signature_hmac_sha256": signature,
            "parameters_schema": parameters_schema or {},
            "run_count": 0,
            "last_run": None,
            "governance": {
                "lint_required": True,
                "denylist_required": True,
                "max_ttl_hours": 168,
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return f"ephemeral_tool_registered:{safe_name}:ttl_hours={ttl_hours}"

    def run_ephemeral_tool(self, tool_name: str, tool_args: dict[str, Any]) -> CommandResult:
        """Execute a previously registered ephemeral tool."""
        return self._run_powershell_file(
            self.ephemeral_runner_ps1,
            {
                "ProjectPath": str(self.project_path),
                "ToolName": tool_name,
                "ToolArgumentsJson": json.dumps(tool_args, ensure_ascii=False),
                "EmitJson": True,
            },
        )

    def list_ephemeral_tools(self) -> list[dict[str, Any]]:
        """Return manifests of all registered ephemeral tools."""
        if not self.tmp_tools_dir.exists():
            return []
        tools: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for manifest_path in sorted(self.tmp_tools_dir.glob("*.json")):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    expires_at_text = str(data.get("expires_at", "")).strip()
                    if expires_at_text:
                        try:
                            expires_at = datetime.fromisoformat(expires_at_text.replace("Z", "+00:00"))
                            data["expired"] = expires_at < now
                        except Exception:
                            data["expired"] = False
                    tools.append(data)
            except Exception:
                continue
        return tools

    # ---------------------------
    # Graph RAG (Neo4j Cypher)
    # ---------------------------
    def query_graph(
        self,
        template_name: str = "",
        params: dict[str, Any] | None = None,
        raw_cypher: str = "",
    ) -> dict[str, Any]:
        """Run a controlled graph query via graph_query.py (template-first)."""
        if not self.graph_query_py.exists():
            return {"error": "graph_query.py-not-found", "results": []}
        import subprocess as _sp  # local import to keep top-level clean
        cmd = [
            self.python_executable,
            str(self.graph_query_py),
            "--project-path", str(self.project_path),
        ]
        if template_name and template_name.strip():
            cmd += ["--template", template_name.strip()]
        elif raw_cypher and raw_cypher.strip():
            cmd += ["--cypher", raw_cypher.strip()]
        else:
            return {"error": "graph-query-requires-template-or-cypher", "results": []}
        if params:
            cmd += ["--params", json.dumps(params, ensure_ascii=False)]
        try:
            proc = _sp.run(
                cmd,
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.memory_timeout_seconds,
            )
        except _sp.TimeoutExpired:
            return {"error": "graph-query-timeout", "results": []}
        try:
            parsed = json.loads(proc.stdout or "{}")
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"error": proc.stderr or "graph-query-failed", "results": []}

    # ---------------------------
    # Tool schemas / tool execution
    # ---------------------------
    def get_tool_definitions(self) -> list[dict[str, Any]]:
        registry = _safe_read_json(self.registry_path)
        roles = registry.get("roles", {})
        role_key = self.agent_name.strip().lower()
        role = roles.get(role_key, roles.get(registry.get("default_role", "default"), {}))

        allowed = set(role.get("allowed_tools", []))
        parent = role.get("extends")
        if parent and parent in roles:
            allowed.update(roles[parent].get("allowed_tools", []))

        tools: list[dict[str, Any]] = []
        if "preflight_reasoner" in allowed:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "preflight_reasoner",
                        "description": "Generate mandatory preflight artifact for the current task.",
                        "parameters": {
                            "type": "object",
                            "properties": {"TaskId": {"type": "string"}},
                            "required": [],
                        },
                    },
                }
            )
        if "step_checkpoint_write" in allowed:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "step_checkpoint_write",
                        "description": "Write step checkpoint.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "StepNumber": {"type": "integer"},
                                "StepName": {"type": "string"},
                                "Status": {"type": "string", "enum": ["running", "ok", "failed", "skipped"]},
                                "Details": {"type": "string"},
                            },
                            "required": ["StepNumber", "StepName", "Status"],
                        },
                    },
                }
            )
        if "delegation_request" in allowed:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "delegation_request",
                        "description": "Request subtask delegation via delegation bus.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "ToAgent": {"type": "string"},
                                "ParentTaskId": {"type": "string"},
                                "Summary": {"type": "string"},
                                "ContextJson": {"type": "string"},
                            },
                            "required": ["ToAgent", "ParentTaskId", "Summary"],
                        },
                    },
                }
            )

        if "powershell_command" in allowed:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "powershell_command",
                        "description": "Run a PowerShell command in project workspace through role-gated dispatcher.",
                        "parameters": {
                            "type": "object",
                            "properties": {"Command": {"type": "string"}},
                            "required": ["Command"],
                        },
                    },
                }
            )
        if "register_ephemeral_tool" in allowed:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "register_ephemeral_tool",
                        "description": (
                            "Forge a new ephemeral tool on-the-fly by writing a PowerShell script to "
                            "workspace/tmp_tools/. The tool becomes immediately available via ephemeral_tool_run. "
                            "Use when the required capability does not exist in the standard tool registry."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "tool_name": {"type": "string", "description": "Unique slug (snake_case)"},
                                "description": {"type": "string", "description": "What the tool does"},
                                "script_body": {"type": "string", "description": "Full PowerShell script content"},
                                "parameters_schema": {
                                    "type": "object",
                                    "description": "Optional JSON Schema for the tool parameters",
                                },
                            },
                            "required": ["tool_name", "description", "script_body"],
                        },
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "ephemeral_tool_run",
                        "description": "Execute a previously forged ephemeral tool by name.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "tool_name": {"type": "string"},
                                "tool_args": {"type": "object", "description": "Named arguments for the tool"},
                            },
                            "required": ["tool_name"],
                        },
                    },
                }
            )
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "list_ephemeral_tools",
                        "description": "List all currently registered ephemeral tools for this project.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            )
        if "graph_query" in allowed:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "graph_query",
                        "description": (
                            "Run a controlled graph query against Neo4j knowledge graph using approved templates. "
                            "Raw Cypher is blocked by default and only allowed when platform policy enables it."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "template_name": {
                                    "type": "string",
                                    "enum": ["module_impact", "file_dependents", "task_risks_open"],
                                    "description": "Approved graph query template",
                                },
                                "params": {"type": "object", "description": "Template parameters"},
                                "raw_cypher": {
                                    "type": "string",
                                    "description": "Optional raw Cypher (allowed only if policy enables raw mode).",
                                },
                            },
                            "required": [],
                        },
                    },
                }
            )
        if "spawn_sub_agent" in allowed:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "spawn_sub_agent",
                        "description": (
                            "Spawn an in-memory micro-LLM sub-agent for a focused sub-task "
                            "(e.g., code review, security audit, docstring generation). "
                            "The sub-agent runs inline and returns its verdict without touching the disk."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "role": {
                                    "type": "string",
                                    "enum": ["reviewer", "security_auditor", "doc_writer", "test_suggester"],
                                    "description": "Sub-agent persona",
                                },
                                "context": {"type": "string", "description": "Code or content for the sub-agent to evaluate"},
                                "instruction": {"type": "string", "description": "What to do with the context"},
                            },
                            "required": ["role", "context", "instruction"],
                        },
                    },
                }
            )
        # Dynamically append registered ephemeral tools from workspace/tmp_tools/
        for et in self.list_ephemeral_tools():
            et_name = str(et.get("tool_name", "")).strip()
            et_desc = str(et.get("description", "Ephemeral tool")).strip()
            et_params = et.get("parameters_schema") or {}
            if not et_name:
                continue
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"ephemeral__{et_name}",
                        "description": f"[Ephemeral] {et_desc}",
                        "parameters": et_params if isinstance(et_params, dict) and et_params else {"type": "object", "properties": {}},
                    },
                }
            )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": "task_complete",
                    "description": "Submit final completion payload for current task.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "files_written": {"type": "array", "items": {"type": "string"}},
                            "tests_passed": {"type": "boolean"},
                            "validation": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["summary", "files_written", "tests_passed"],
                    },
                },
            }
        )
        return tools

    def handle_tool_call(self, task_id: str, tool_name: str, args: dict[str, Any]) -> str:
        if tool_name == "powershell_command":
            result = self.dispatch_tool("powershell_command", args)
            if not result.success:
                return f"error: {result.error or result.output}"
            return (result.output or "").strip() or "(no output)"

        if tool_name == "preflight_reasoner":
            target_task = str(args.get("TaskId", "")).strip() or task_id
            result = self.generate_preflight(target_task)
            if not result.success:
                return f"error: {result.error or result.output}"
            payload = result.json or {}
            return f"preflight_generated:{payload.get('preflight_path', '')}"

        if tool_name == "step_checkpoint_write":
            step_number = int(args.get("StepNumber", 0) or 0)
            step_name = str(args.get("StepName", "")).strip()
            status = str(args.get("Status", "")).strip() or "running"
            details = str(args.get("Details", "")).strip()
            result = self.write_step_checkpoint(task_id, step_number, step_name, status, details=details)
            if not result.success:
                return f"error: {result.error or result.output}"
            return "checkpoint_written"

        if tool_name == "delegation_request":
            result = self.dispatch_tool("delegation_request", args)
            if not result.success:
                return f"error: {result.error or result.output}"
            return (result.output or "").strip() or "delegation_requested"

        if tool_name == "task_complete":
            return "__TASK_COMPLETE_SIGNAL__"

        if tool_name == "register_ephemeral_tool":
            result = self.register_ephemeral_tool(
                tool_name=str(args.get("tool_name", "")),
                description=str(args.get("description", "")),
                script_body=str(args.get("script_body", "")),
                parameters_schema=args.get("parameters_schema"),
            )
            return result

        if tool_name == "ephemeral_tool_run":
            et_name = str(args.get("tool_name", "")).strip()
            et_args = args.get("tool_args") or {}
            if not isinstance(et_args, dict):
                et_args = {}
            result = self.run_ephemeral_tool(et_name, et_args)
            if not result.success:
                return f"error: {result.error or result.output}"
            return (result.output or "").strip() or "(no output)"

        if tool_name == "list_ephemeral_tools":
            tools_list = self.list_ephemeral_tools()
            return json.dumps(tools_list, ensure_ascii=False, indent=2)

        if tool_name == "graph_query":
            template_name = str(args.get("template_name", "")).strip()
            params = args.get("params") or {}
            raw_cypher = str(args.get("raw_cypher", "")).strip()
            result = self.query_graph(
                template_name=template_name,
                params=params if isinstance(params, dict) else {},
                raw_cypher=raw_cypher,
            )
            return json.dumps(result, ensure_ascii=False, indent=2)

        if tool_name == "spawn_sub_agent":
            # Handled by Run-AgentLoop.py which injects spawn_sub_agent_fn into worker
            handler = getattr(self, "_spawn_sub_agent_fn", None)
            if callable(handler):
                return handler(args)
            return "error: spawn_sub_agent-not-wired"

        # Dynamic ephemeral tool dispatch (prefix: ephemeral__)
        if tool_name.startswith("ephemeral__"):
            et_name = tool_name[len("ephemeral__"):]
            result = self.run_ephemeral_tool(et_name, args)
            if not result.success:
                return f"error: {result.error or result.output}"
            return (result.output or "").strip() or "(no output)"

        return f"error: unsupported-tool:{tool_name}"
