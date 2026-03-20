from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any

from llm_client import LLMClient
from agent_worker import AgentWorker


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("v2-native-runtime")


SYSTEM_PROMPT = """
You are operating inside the Orchestrator V5 native runtime.

Rules:
1) Use tools for actions; do not invent filesystem state.
2) Keep changes minimal and aligned to task acceptance criteria.
3) Always validate before completing.
4) Call task_complete only when work is actually done.
5) If schema validation fails, fix and retry.
6) Use spawn_sub_agent for inline peer-review or security audit before completing.
""".strip()


SUB_AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    "reviewer": (
        "You are a senior code reviewer. Analyze the provided code for correctness, "
        "clarity, and adherence to best practices. Return a JSON with keys: "
        "'approved' (bool), 'issues' (list of strings), 'suggestions' (list of strings)."
    ),
    "security_auditor": (
        "You are a security engineer. Audit the provided code for OWASP Top 10 "
        "vulnerabilities, injection risks, and authentication/authorization flaws. "
        "Return JSON: 'safe' (bool), 'vulnerabilities' (list), 'severity' (low/medium/high/critical)."
    ),
    "doc_writer": (
        "You are a technical writer. Write concise docstrings and inline comments for "
        "the provided code. Return JSON: 'docstring' (string), 'inline_comments' (list of strings)."
    ),
    "test_suggester": (
        "You are a QA engineer. Suggest test cases for the provided code. "
        "Return JSON: 'test_cases' (list of {name, description, type}) where type is unit/integration/e2e."
    ),
}


def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _task_priority_score(task: dict[str, Any]) -> int:
    priority = str(task.get("priority", "")).strip().upper()
    if priority == "P0":
        return 4
    if priority == "P1":
        return 3
    if priority == "P2":
        return 2
    if priority == "P3":
        return 1
    return 1


def _task_file_count(task: dict[str, Any]) -> int:
    files_raw = task.get("files_affected", [])
    if isinstance(files_raw, list):
        return len([x for x in files_raw if str(x).strip()])
    return 0


def _is_infra_heavy_task(task: dict[str, Any]) -> bool:
    task_id = str(task.get("id", "")).strip().upper()
    if task_id.startswith("REPAIR-DEPLOY-"):
        return True
    if task_id.startswith("REPAIR-TEST-FAIL-"):
        return True
    if task_id.startswith("COBERTURA-FALHA-"):
        return True

    files_raw = task.get("files_affected", [])
    if not isinstance(files_raw, list):
        return False

    infra_markers = (
        "ai-orchestrator/docker",
        "docker-compose",
        "k8s/",
        "kubernetes/",
        "infra/",
        ".github/workflows",
        "database/migrations",
        ".env",
    )
    for path_value in files_raw:
        normalized = str(path_value).replace("\\", "/").strip().lower()
        if not normalized:
            continue
        for marker in infra_markers:
            if marker in normalized:
                return True
    return False


def _select_model_for_task(task: dict[str, Any], default_model: str) -> tuple[str, str]:
    routing_enabled = _env_bool("ORCHESTRATOR_LLM_ROUTING_ENABLED", True)
    fast_model = (os.getenv("ORCHESTRATOR_LLM_MODEL_FAST") or default_model or "").strip()
    heavy_model = (os.getenv("ORCHESTRATOR_LLM_MODEL_HEAVY") or "").strip()
    threshold_raw = (os.getenv("ORCHESTRATOR_LLM_ROUTING_COMPLEXITY_THRESHOLD") or "12").strip()
    try:
        threshold = max(int(threshold_raw), 1)
    except ValueError:
        threshold = 8

    if not routing_enabled:
        return fast_model or default_model, "routing-disabled"
    if _is_infra_heavy_task(task):
        return fast_model or default_model, "infra-default"

    file_count = _task_file_count(task)
    deps_raw = task.get("dependencies", [])
    dep_count = len(deps_raw) if isinstance(deps_raw, list) else 0
    desc = str(task.get("description", "") or "")
    complexity = _task_priority_score(task) + file_count + dep_count + (len(desc) // 600)

    if heavy_model and complexity >= threshold:
        return heavy_model, "heavy"
    return fast_model or default_model, "fast"


def _build_memory_snippet(memory_doc: dict[str, Any]) -> str:
    if not memory_doc:
        return ""
    results = memory_doc.get("results", [])
    if not isinstance(results, list) or not results:
        return ""
    lines: list[str] = []
    for item in results[:3]:
        title = str(item.get("title", "")).strip()
        path = str(item.get("path", "")).strip()
        snippet = str(item.get("snippet", "")).strip().replace("\n", " ")
        score = item.get("score", "")
        lines.append(f"- [{score}] {title} ({path}) :: {snippet[:180]}")
    return "\n".join(lines)


def _assistant_message_from_response(response: dict[str, Any]) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.get("content", "") or "",
    }
    tool_calls = response.get("tool_calls", []) or []
    if tool_calls:
        normalized_calls = []
        for call in tool_calls:
            normalized_calls.append(
                {
                    "id": call.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": call.get("name") or "",
                        "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                    },
                }
            )
        message["tool_calls"] = normalized_calls
    return message


def _handle_task_complete(
    worker: AgentWorker,
    task: dict[str, Any],
    task_complete_args: dict[str, Any],
) -> tuple[bool, str]:
    task_id = str(task.get("id", "")).strip()
    summary = str(task_complete_args.get("summary", "")).strip()
    files_written_raw = task_complete_args.get("files_written", [])
    files_written = []
    if isinstance(files_written_raw, list):
        files_written = [str(x) for x in files_written_raw if str(x).strip()]
    tests_passed = bool(task_complete_args.get("tests_passed", False))
    validation_raw = task_complete_args.get("validation", [])
    validation = []
    if isinstance(validation_raw, list):
        validation = [str(x) for x in validation_raw if str(x).strip()]

    payload = worker.build_completion_payload(
        task=task,
        summary=summary,
        files_written=files_written,
        tests_passed=tests_passed,
        validation=validation,
    )
    payload_path = worker.save_completion_payload(task_id, payload)

    worker.write_step_checkpoint(task_id, 3, "validate-complete", "running")
    schema_result = worker.validate_completion_payload(payload_path)
    schema_json = schema_result.json or {}
    if (not schema_result.success) or (not bool(schema_json.get("success", False))):
        errors = schema_json.get("errors", []) if isinstance(schema_json, dict) else []
        reason = "output-schema-invalid"
        if errors:
            reason = f"{reason}:{','.join([str(e) for e in errors])}"
        worker.write_step_checkpoint(task_id, 3, "validate-complete", "failed", error_text=reason)
        return False, reason

    complete_result = worker.complete_task(
        task_id=task_id,
        payload_path=payload_path,
        notes=str(payload.get("summary", "")),
        artifacts=[str(x) for x in payload.get("files_written", [])],
    )
    if not complete_result.success:
        reason = complete_result.error or complete_result.output or "complete-failed"
        worker.write_step_checkpoint(task_id, 3, "validate-complete", "failed", error_text=_truncate(reason, 400))
        return False, reason

    worker.write_step_checkpoint(task_id, 3, "validate-complete", "ok")
    worker.clear_step_checkpoints(task_id)
    return True, ""


def _spawn_sub_agent(llm: LLMClient, args: dict[str, Any]) -> str:
    """
    Run an in-memory micro-LLM sub-agent for a focused sub-task.
    The sub-agent gets a single user message and returns its verdict.
    No disk I/O — pure LLM call within the parent's execution context.
    """
    role = str(args.get("role", "reviewer")).strip()
    context = str(args.get("context", "")).strip()
    instruction = str(args.get("instruction", "")).strip()
    budget = args.get("__budget__", {})
    if not isinstance(budget, dict):
        budget = {}

    max_calls = int(budget.get("max_calls", 2))
    max_seconds = int(budget.get("max_seconds", 45))
    max_tokens = int(budget.get("max_tokens", 2000))
    max_usd = float(budget.get("max_usd", 0.05))
    calls_used = int(budget.get("calls_used", 0))
    tokens_used = int(budget.get("tokens_used", 0))
    usd_used = float(budget.get("usd_used", 0.0))

    if calls_used >= max_calls:
        return "error: swarm-budget-exceeded:max-calls"
    if tokens_used >= max_tokens:
        return "error: swarm-budget-exceeded:max-tokens"
    if usd_used >= max_usd:
        return "error: swarm-budget-exceeded:max-usd"

    system_prompt = SUB_AGENT_SYSTEM_PROMPTS.get(role, SUB_AGENT_SYSTEM_PROMPTS["reviewer"])
    user_content = f"INSTRUCTION: {instruction}\n\nCONTEXT:\n{_truncate(context, 4000)}"

    try:
        response = llm.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            timeout_seconds=max_seconds,
            max_tokens_override=min(800, max_tokens),
        )
        verdict = (response.get("content") or "").strip()
        usage = response.get("usage") if isinstance(response, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        used_tokens_now = int(usage.get("total_tokens", 0) or 0)
        used_usd_now = float(response.get("cost_usd") or 0.0)

        budget["calls_used"] = calls_used + 1
        budget["tokens_used"] = tokens_used + used_tokens_now
        budget["usd_used"] = round(usd_used + used_usd_now, 8)
        budget["last_usage"] = {
            "total_tokens": used_tokens_now,
            "cost_usd": used_usd_now,
        }

        if int(budget.get("tokens_used", 0)) > max_tokens:
            return "error: swarm-budget-exceeded:max-tokens-postcall"
        if float(budget.get("usd_used", 0.0)) > max_usd:
            return "error: swarm-budget-exceeded:max-usd-postcall"

        return json.dumps(
            {
                "sub_agent_role": role,
                "verdict": verdict,
                "usage": usage,
                "cost_usd": used_usd_now,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"sub_agent_role": role, "error": str(exc)}, ensure_ascii=False)


def run_task(
    worker: AgentWorker,
    llm: LLMClient,
    task: dict[str, Any],
    max_turns: int,
) -> tuple[bool, str, dict[str, Any]]:
    task_id = str(task.get("id", "")).strip()
    task_title = str(task.get("title", "")).strip()
    task_desc = str(task.get("description", "")).strip()
    llm_metrics: dict[str, Any] = {
        "llm_calls": 0,
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        "llm_total_tokens": 0,
        "llm_elapsed_seconds": 0.0,
        "llm_model": llm.model,
    }

    worker.write_step_checkpoint(task_id, 1, "preflight", "running")
    preflight_result = worker.generate_preflight(task_id)
    if not preflight_result.success:
        reason = preflight_result.error or preflight_result.output or "preflight-failed"
        worker.write_step_checkpoint(task_id, 1, "preflight", "failed", error_text=_truncate(reason, 400))
        return False, reason, llm_metrics
    worker.write_step_checkpoint(task_id, 1, "preflight", "ok")
    worker.write_step_checkpoint(task_id, 2, "execute", "running")

    memory_query = task_desc or task_title or task_id
    memory_doc = worker.query_lessons(memory_query, top_k=5)
    memory_context = _build_memory_snippet(memory_doc)

    tools = worker.get_tool_definitions()
    swarm_budget: dict[str, Any] = {
        "max_calls": int(os.getenv("ORCHESTRATOR_SWARM_MAX_SUB_AGENTS", "2")),
        "max_seconds": int(os.getenv("ORCHESTRATOR_SWARM_MAX_SUB_AGENT_SECONDS", "45")),
        "max_tokens": int(os.getenv("ORCHESTRATOR_SWARM_MAX_SUB_AGENT_TOKENS", "2000")),
        "max_usd": float(os.getenv("ORCHESTRATOR_SWARM_MAX_SUB_AGENT_USD", "0.05")),
        "calls_used": 0,
        "tokens_used": 0,
        "usd_used": 0.0,
    }
    cancel_on_budget = os.getenv("ORCHESTRATOR_SWARM_CANCEL_ON_BUDGET", "1").strip().lower() in {"1", "true", "yes", "on"}

    system_prompt = llm.parse_system_prompt(
        SYSTEM_PROMPT,
        {
            "task": json.dumps(task, ensure_ascii=False, indent=2),
            "memory": memory_context,
        },
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Execute task {task_id} ({task_title}). "
                "First generate/confirm preflight, then work, then call task_complete."
            ),
        },
    ]

    for turn in range(1, max_turns + 1):
        llm_call_started = time.perf_counter()
        response = llm.generate_response(messages, tools=tools)
        llm_elapsed = max(time.perf_counter() - llm_call_started, 0.0)
        llm_metrics["llm_calls"] += 1
        llm_metrics["llm_elapsed_seconds"] = float(llm_metrics["llm_elapsed_seconds"]) + llm_elapsed
        usage = response.get("usage") if isinstance(response, dict) else {}
        if isinstance(usage, dict):
            llm_metrics["llm_prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            llm_metrics["llm_completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            llm_metrics["llm_total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        messages.append(_assistant_message_from_response(response))
        tool_calls = response.get("tool_calls", []) or []

        if not tool_calls:
            messages.append(
                {
                    "role": "user",
                    "content": "No tool was called. Continue using tools or call task_complete with final payload.",
                }
            )
            continue

        for call in tool_calls:
            tool_name = str(call.get("name", "")).strip()
            tool_args = call.get("arguments", {}) if isinstance(call.get("arguments"), dict) else {}
            tool_call_id = str(call.get("id", "")).strip()

            if tool_name == "task_complete":
                ok, reason = _handle_task_complete(worker, task, tool_args)
                if ok:
                    worker.write_step_checkpoint(task_id, 2, "execute", "ok")
                    return True, "", llm_metrics

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps({"success": False, "error": reason}, ensure_ascii=False),
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"Completion rejected by orchestrator: {reason}. Fix and call task_complete again.",
                    }
                )
                continue

            if tool_name == "spawn_sub_agent":
                local_args = dict(tool_args) if isinstance(tool_args, dict) else {}
                local_args["__budget__"] = swarm_budget
                observation = worker.handle_tool_call(task_id, tool_name, local_args)
            else:
                observation = worker.handle_tool_call(task_id, tool_name, tool_args)
            tool_success = not observation.strip().lower().startswith("error:")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": json.dumps({"success": tool_success, "observation": _truncate(observation, 6000)}, ensure_ascii=False),
                }
            )
            if not tool_success:
                if tool_name == "spawn_sub_agent" and "swarm-budget-exceeded" in observation and cancel_on_budget:
                    worker.write_step_checkpoint(task_id, 2, "execute", "failed", error_text="swarm-budget-exceeded")
                    return False, "swarm-budget-exceeded", llm_metrics
                messages.append(
                    {
                        "role": "user",
                        "content": "Tool execution failed. Replan and retry with safer arguments or another tool.",
                    }
                )

    worker.write_step_checkpoint(task_id, 2, "execute", "failed", error_text="max-turns-reached")
    return False, "max-turns-reached", llm_metrics


def main() -> int:
    parser = argparse.ArgumentParser(description="Native Python cognitive runtime for Orchestrator V2/V4.")
    parser.add_argument("--project_path", required=True)
    parser.add_argument("--agent_name", required=True)
    parser.add_argument("--max_tasks_per_cycle", type=int, default=2)
    parser.add_argument("--max_turns", type=int, default=12)
    parser.add_argument("--python_executable", default="python")
    parser.add_argument("--force_task_id", default="")
    parser.add_argument("--emit_json", action="store_true")
    args = parser.parse_args()

    worker = AgentWorker(args.project_path, args.agent_name, python_executable=args.python_executable)
    llm = LLMClient()

    # Wire swarm handler: sub-agent spawning uses the same LLM client inline
    worker._spawn_sub_agent_fn = lambda a: _spawn_sub_agent(llm, a)  # type: ignore[attr-defined]

    stats = {
        "success": True,
        "agent_name": args.agent_name,
        "llm_enabled": llm.is_enabled(),
        "llm_disabled_reason": llm.get_disabled_reason(),
        "llm_model": llm.model,
        "llm_models_used": [],
        "executed_tasks": 0,
        "completed_tasks": 0,
        "failed_tasks": 0,
        "skipped_non_native": 0,
        "skipped_no_llm": 0,
        "llm_calls": 0,
        "llm_prompt_tokens": 0,
        "llm_completion_tokens": 0,
        "llm_total_tokens": 0,
        "llm_elapsed_seconds": 0.0,
        "llm_tokens_per_second": 0.0,
        "llm_route_fast": 0,
        "llm_route_heavy": 0,
        "llm_route_routing_disabled": 0,
        "llm_route_infra_default": 0,
        "errors": [],
    }

    tasks = worker.list_assigned_in_progress_tasks(max_tasks=max(1, args.max_tasks_per_cycle))
    if args.force_task_id:
        tasks = [t for t in tasks if str(t.get("id", "")).strip() == args.force_task_id.strip()]

    if not tasks:
        if args.emit_json:
            print(json.dumps(stats, ensure_ascii=False))
        return 0

    for task in tasks:
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            continue
        if not worker.is_native_task(task):
            stats["skipped_non_native"] += 1
            continue
        selected_model, selected_route = _select_model_for_task(task=task, default_model=llm.model)
        task_llm = LLMClient(model_override=selected_model)
        if not task_llm.is_enabled():
            stats["skipped_no_llm"] += 1
            continue
        route_counter_name = f"llm_route_{selected_route.replace('-', '_')}"
        if route_counter_name in stats:
            stats[route_counter_name] += 1
        if selected_model and selected_model not in stats["llm_models_used"]:
            stats["llm_models_used"].append(selected_model)

        stats["executed_tasks"] += 1
        ok, reason, llm_metrics = run_task(worker=worker, llm=task_llm, task=task, max_turns=max(1, args.max_turns))
        stats["llm_model"] = str(llm_metrics.get("llm_model", selected_model))
        stats["llm_calls"] += int(llm_metrics.get("llm_calls", 0) or 0)
        stats["llm_prompt_tokens"] += int(llm_metrics.get("llm_prompt_tokens", 0) or 0)
        stats["llm_completion_tokens"] += int(llm_metrics.get("llm_completion_tokens", 0) or 0)
        stats["llm_total_tokens"] += int(llm_metrics.get("llm_total_tokens", 0) or 0)
        stats["llm_elapsed_seconds"] = float(stats.get("llm_elapsed_seconds", 0.0)) + float(llm_metrics.get("llm_elapsed_seconds", 0.0) or 0.0)
        if ok:
            stats["completed_tasks"] += 1
        else:
            stats["failed_tasks"] += 1
            stats["success"] = False
            stats["errors"].append(f"{task_id}:{reason}")

    llm_elapsed_seconds = max(float(stats.get("llm_elapsed_seconds", 0.0) or 0.0), 0.0001)
    llm_total_tokens = int(stats.get("llm_total_tokens", 0) or 0)
    stats["llm_elapsed_seconds"] = round(llm_elapsed_seconds, 4) if llm_total_tokens > 0 else 0.0
    stats["llm_tokens_per_second"] = round(llm_total_tokens / llm_elapsed_seconds, 2) if llm_total_tokens > 0 else 0.0

    if args.emit_json:
        print(json.dumps(stats, ensure_ascii=False))
    else:
        logger.info(
            "Native runtime finished: executed=%s completed=%s failed=%s skipped_non_native=%s skipped_no_llm=%s",
            stats["executed_tasks"],
            stats["completed_tasks"],
            stats["failed_tasks"],
            stats["skipped_non_native"],
            stats["skipped_no_llm"],
        )

    # Return non-zero only for hard failures when task execution actually failed.
    if stats["failed_tasks"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
