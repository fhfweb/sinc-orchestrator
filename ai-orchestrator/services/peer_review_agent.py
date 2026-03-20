"""
Peer Review Agent — LangGraph Multi-Agent Service
===================================================
Implements a Code → Review → Fix cycle using LangGraph state machines.

Architecture
------------
  coder_node   — generates / modifies code for the task
  reviewer_node — reviews the output, scores quality (0.0–1.0)
  fix_node      — applies reviewer suggestions if score < REVIEW_THRESHOLD
  done_node     — posts completion to orchestrator API

LangGraph graph:
  START → coder → reviewer → (score ≥ threshold?) → done
                            ↘ fix → reviewer (max MAX_REVIEW_CYCLES)

Environment variables
---------------------
ORCHESTRATOR_URL        — http://<host>:8765
ORCHESTRATOR_API_KEY    — API key
PROJECT_ID              — project to claim tasks from
TENANT_ID               — tenant identifier
AGENT_NAME              — identity (default peer-review-agent)
ANTHROPIC_API_KEY       — Claude API key (required for LLM calls)
REVIEW_THRESHOLD        — quality score to accept (default 0.75)
MAX_REVIEW_CYCLES       — max coder→reviewer→fix iterations (default 3)
AGENT_POLL_INTERVAL     — seconds between polls (default 20)
PEER_REVIEW_TAGS        — comma-separated task tags that trigger peer review
                          (default "review,security,critical,P0")

Port: none (pure worker, no HTTP server)
"""

from __future__ import annotations
from services.streaming.core.config import env_get

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, TypedDict

# ── LangGraph ─────────────────────────────────────────────────────────────────
try:
    from langgraph.graph import END, START, StateGraph
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False
    print("[peer-review] langgraph not installed — worker will idle", flush=True)

# ── Anthropic ─────────────────────────────────────────────────────────────────
try:
    import anthropic as _anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

# ── httpx ─────────────────────────────────────────────────────────────────────
import httpx as _httpx
from services.http_client import create_sync_resilient_client

# ── OpenTelemetry ─────────────────────────────────────────────────────────────
try:
    from services.otel_setup import configure_otel, span as _span
    configure_otel("peer-review-agent")
except ImportError:
    from contextlib import nullcontext
    def _span(_name: str, **_kw):  # type: ignore[misc]
        return nullcontext()


# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────

ORCHESTRATOR_URL     = env_get("ORCHESTRATOR_URL", default="").rstrip("/")
ORCHESTRATOR_API_KEY = env_get("ORCHESTRATOR_API_KEY", default="")
PROJECT_ID           = env_get("PROJECT_ID", default="")
TENANT_ID            = env_get("TENANT_ID", default="local")
AGENT_NAME           = env_get("AGENT_NAME", default="peer-review-agent")
ANTHROPIC_API_KEY    = env_get("ANTHROPIC_API_KEY", default="")
CLAUDE_MODEL         = env_get("AGENT_MODEL", default="claude-sonnet-4-6")
REVIEW_THRESHOLD     = float(env_get("REVIEW_THRESHOLD", default="0.75"))
MAX_REVIEW_CYCLES    = int(env_get("MAX_REVIEW_CYCLES", default="3"))
POLL_INTERVAL        = int(env_get("AGENT_POLL_INTERVAL", default="20"))
PEER_REVIEW_TAGS     = {t.strip() for t in
                        env_get("PEER_REVIEW_TAGS", default="review,security,critical,P0").split(",")}


# ──────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR API CLIENT
# ──────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str):
    print(f"[{_now()}] [peer-review] {msg}", flush=True)


_http: Any = None


def _get_http():
    global _http
    if _http is None:
        _http = create_sync_resilient_client(
            service_name="peer-review-agent",
            base_url=ORCHESTRATOR_URL,
            headers={
                "X-Api-Key":    ORCHESTRATOR_API_KEY,
                "X-Tenant-ID":  TENANT_ID,
                "Content-Type": "application/json",
            },
            timeout=_httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        )
    return _http


def _api(method: str, path: str, body: dict | None = None) -> dict:
    client = _get_http()
    resp = client.request(method, path, json=body)
    resp.raise_for_status()
    return resp.json() if resp.content else {}

def _claim_task() -> dict | None:
    """Claim one pending task.

    Strategy:
    1. Try /queue/poll (long-poll — blocks up to 30 s server-side, very efficient).
    2. On success return a minimal task dict; caller only needs 'id'.
    3. Fall back to /tasks/claim for servers that support tag-filtered claiming.
    """
    # ── Long-poll attempt ──────────────────────────────────────────────────
    try:
        resp = _api("POST", "/queue/poll", {
            "agent_name":  AGENT_NAME,
            "claim_ttl_s": 300,
            "timeout_s":   30,
        })
        task = resp.get("task")
        if task:
            _log(f"Claimed task {task.get('id')} via long-poll")
            return task
        task_id = resp.get("task_id")
        if task_id:
            _log(f"Claimed task {task_id} via long-poll")
            return {"id": task_id}
    except Exception:
        pass  # endpoint may not exist on older orchestrators — fall through

    # ── Legacy claim with tag filter ──────────────────────────────────────
    try:
        resp = _api("POST", "/tasks/claim", {
            "agent_name":   AGENT_NAME,
            "project_id":   PROJECT_ID,
            "claim_ttl_s":  300,
            "tags":         list(PEER_REVIEW_TAGS),
        })
        task = resp.get("task")
        if task:
            _log(f"Claimed task {task.get('id')} — {task.get('title', '')[:60]}")
        return task
    except Exception as e:
        _log(f"Claim error: {e}")
        return None


def _heartbeat(task_id: str, pct: int, step: str):
    try:
        _api("POST", f"/tasks/{task_id}/heartbeat",
             {"agent": AGENT_NAME, "progress": pct, "step": step, "metadata": {"source": "peer-review-agent"}})
    except Exception:
        pass


def _complete_task(task_id: str, status: str, summary: str,
                   files_modified: list[str], review_cycles: int):
    try:
        _api("POST", "/tasks/complete", {
            "task_id":         task_id,
            "status":          status,
            "summary":         summary,
            "files_modified":  files_modified,
            "backend_used":    f"langgraph+claude/{review_cycles}cycles",
            "tests_passed":    status == "success",
            "agent_name":      AGENT_NAME,
        })
    except Exception as e:
        _log(f"Complete error: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# LLM HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _llm(system: str, user: str, max_tokens: int = 4096) -> str:
    """Call Claude and return text response."""
    if not _HAS_ANTHROPIC or not ANTHROPIC_API_KEY:
        return "LLM unavailable — ANTHROPIC_API_KEY not set"
    client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg    = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text if msg.content else ""


def _extract_score(text: str) -> float:
    """Parse quality score from reviewer output (looks for SCORE: 0.xx)."""
    m = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if m:
        try:
            return min(1.0, max(0.0, float(m.group(1))))
        except ValueError:
            pass
    # Fallback: count positive/negative keywords
    text_l = text.lower()
    positives = sum(1 for w in ("correct", "good", "pass", "approve", "lgtm") if w in text_l)
    negatives  = sum(1 for w in ("bug", "error", "issue", "fail", "wrong", "miss") if w in text_l)
    if positives + negatives == 0:
        return 0.6  # neutral
    return min(1.0, max(0.0, positives / (positives + negatives)))


# ──────────────────────────────────────────────────────────────────────────────
# LANGGRAPH STATE
# ──────────────────────────────────────────────────────────────────────────────

class ReviewState(TypedDict):
    task:           dict          # full task dict from orchestrator
    code_output:    str           # latest generated/modified code
    review_output:  str           # latest reviewer feedback
    review_score:   float         # 0.0–1.0
    review_cycles:  int           # iterations so far
    files_modified: list[str]
    final_status:   str           # "success" | "partial" | "failed"
    final_summary:  str


# ──────────────────────────────────────────────────────────────────────────────
# GRAPH NODES
# ──────────────────────────────────────────────────────────────────────────────

def coder_node(state: ReviewState) -> ReviewState:
    """Generate or modify code for the task."""
    task     = state["task"]
    title    = task.get("title", "")
    desc     = task.get("description", "")
    previous = state.get("review_output", "")
    cycle    = state.get("review_cycles", 0)

    _heartbeat(task["id"], 10 + cycle * 20, f"coder cycle {cycle + 1}")

    with _span("peer_review.coder", task_id=task["id"], cycle=cycle):
        system = (
            "You are an expert software engineer performing a coding task. "
            "Write clean, production-ready code. "
            "If given reviewer feedback, address ALL points before resubmitting. "
            "End your response with a section '## Files Modified' listing changed files."
        )
        prev_section = (
            f"\n\n## Reviewer Feedback (cycle {cycle})\n{previous}"
            if previous else ""
        )
        user = (
            f"## Task: {title}\n\n{desc}"
            f"{prev_section}\n\n"
            "Implement the changes. Show the complete modified files."
        )
        code_output = _llm(system, user, max_tokens=8192)

    # Extract files mentioned in ## Files Modified
    files: list[str] = []
    in_section = False
    for line in code_output.splitlines():
        if "## Files Modified" in line:
            in_section = True
            continue
        if in_section:
            stripped = line.strip().lstrip("-* ")
            if stripped and not stripped.startswith("#"):
                files.append(stripped)
            elif stripped.startswith("#"):
                break

    return {
        **state,
        "code_output":    code_output,
        "files_modified": files or state.get("files_modified", []),
        "review_cycles":  cycle + 1,
    }


def reviewer_node(state: ReviewState) -> ReviewState:
    """Review the code output and produce a quality score."""
    task        = state["task"]
    code_output = state.get("code_output", "")
    cycle       = state.get("review_cycles", 1)

    _heartbeat(task["id"], 30 + cycle * 20, f"reviewer cycle {cycle}")

    with _span("peer_review.reviewer", task_id=task["id"], cycle=cycle):
        system = (
            "You are a senior code reviewer. Your job is to critically evaluate "
            "the implementation quality, correctness, security, and adherence to "
            "the task requirements.\n\n"
            "MANDATORY: Your response MUST end with a line in EXACTLY this format "
            "(no other text on that line):\n"
            "SCORE: 0.85\n"
            "The score must be a decimal number between 0.0 and 1.0. "
            "Do not write SCORE: X/10 or any other format."
        )
        user = (
            f"## Original Task\n{task.get('title', '')}\n\n"
            f"{task.get('description', '')}\n\n"
            f"## Implementation (cycle {cycle})\n{code_output}\n\n"
            "Review the implementation thoroughly. Identify bugs, missing requirements, "
            "security issues, or style problems. Then end your review with:\n"
            "SCORE: <decimal 0.0-1.0>"
        )
        review_output = _llm(system, user, max_tokens=4096)
        score         = _extract_score(review_output)

    _log(f"  [{task['id']}] cycle={cycle} review_score={score:.2f} "
         f"threshold={REVIEW_THRESHOLD}")

    return {
        **state,
        "review_output": review_output,
        "review_score":  score,
    }


def fix_node(state: ReviewState) -> ReviewState:
    """Lightweight fix pass — re-runs coder with reviewer context."""
    # fix_node just returns to coder with current review context — coder handles fixes
    return state


def done_node(state: ReviewState) -> ReviewState:
    """Determine final status and post completion to orchestrator."""
    score  = state.get("review_score", 0.0)
    cycles = state.get("review_cycles", 1)
    task   = state["task"]

    if score >= REVIEW_THRESHOLD:
        final_status  = "success"
        final_summary = (
            f"Peer review passed (score={score:.2f}, cycles={cycles}). "
            f"{state.get('review_output', '')[:300]}"
        )
    elif score >= 0.5:
        final_status  = "partial"
        final_summary = (
            f"Partial completion (score={score:.2f}, cycles={cycles}). "
            "Review threshold not met but partial output provided."
        )
    else:
        final_status  = "failed"
        final_summary = (
            f"Peer review failed (score={score:.2f}, cycles={cycles}). "
            f"Reviewer: {state.get('review_output', '')[:300]}"
        )

    _heartbeat(task["id"], 95, "posting completion")
    _complete_task(
        task["id"], final_status, final_summary,
        state.get("files_modified", []), cycles,
    )
    _log(f"  [{task['id']}] done status={final_status} score={score:.2f} "
         f"cycles={cycles}")

    return {**state, "final_status": final_status, "final_summary": final_summary}


# ──────────────────────────────────────────────────────────────────────────────
# ROUTING
# ──────────────────────────────────────────────────────────────────────────────

def _route_after_review(state: ReviewState) -> str:
    """After reviewer: go to done if score ≥ threshold or max cycles reached."""
    if (state["review_score"] >= REVIEW_THRESHOLD
            or state["review_cycles"] >= MAX_REVIEW_CYCLES):
        return "done"
    return "fix"


# ──────────────────────────────────────────────────────────────────────────────
# BUILD GRAPH
# ──────────────────────────────────────────────────────────────────────────────

def _build_graph():
    if not _HAS_LANGGRAPH:
        return None
    g = StateGraph(ReviewState)
    g.add_node("coder",    coder_node)
    g.add_node("reviewer", reviewer_node)
    g.add_node("fix",      fix_node)
    g.add_node("done",     done_node)

    g.add_edge(START,      "coder")
    g.add_edge("coder",    "reviewer")
    g.add_conditional_edges("reviewer", _route_after_review,
                            {"done": "done", "fix": "fix"})
    g.add_edge("fix",      "coder")
    g.add_edge("done",     END)

    return g.compile()


_GRAPH = _build_graph()


# ──────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────────────────────────

def _run_task(task: dict) -> None:
    """Execute peer-review pipeline for a single task."""
    if _GRAPH is None:
        _log("LangGraph not available — skipping")
        return

    initial: ReviewState = {
        "task":           task,
        "code_output":    "",
        "review_output":  "",
        "review_score":   0.0,
        "review_cycles":  0,
        "files_modified": [],
        "final_status":   "",
        "final_summary":  "",
    }

    with _span("peer_review.pipeline", task_id=task.get("id", "")):
        try:
            _GRAPH.invoke(initial)
        except Exception as e:
            _log(f"  [{task.get('id')}] pipeline error: {e}")
            _complete_task(task["id"], "failed",
                           f"Pipeline error: {e}", [], 0)


def main():
    if not _HAS_LANGGRAPH:
        _log("LangGraph not installed. Install: pip install langgraph")
        _log("Idling — will retry if langgraph becomes available on restart.")
        while True:
            time.sleep(60)

    if not ORCHESTRATOR_URL:
        _log("ORCHESTRATOR_URL not set — exiting")
        sys.exit(1)

    if not ORCHESTRATOR_API_KEY:
        _log("ORCHESTRATOR_API_KEY not set — exiting")
        sys.exit(1)

    if not ANTHROPIC_API_KEY:
        _log("WARNING: ANTHROPIC_API_KEY not set — LLM calls will fail")

    _log(f"Peer Review Agent starting")
    _log(f"  orchestrator = {ORCHESTRATOR_URL}")
    _log(f"  project      = {PROJECT_ID}")
    _log(f"  model        = {CLAUDE_MODEL}")
    _log(f"  threshold    = {REVIEW_THRESHOLD}")
    _log(f"  max_cycles   = {MAX_REVIEW_CYCLES}")
    _log(f"  tags         = {sorted(PEER_REVIEW_TAGS)}")

    while True:
        try:
            task = _claim_task()
            if task:
                _run_task(task)
            else:
                # Long-poll already waited; short sleep avoids tight spin on errors
                time.sleep(1)
        except Exception as e:
            _log(f"Poll error: {e}")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()

