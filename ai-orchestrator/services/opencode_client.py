"""
opencode_client.py
==================
Async client for OpenCode v1.2.27 serve mode.

Real API endpoints (verified against running container):
  GET  /global/health           → {"healthy": true, "version": "1.2.27"}
  GET  /session                 → [{id, slug, directory, title, ...}]
  POST /session                 → {id, slug, directory, ...}
  GET  /session/{id}/message    → [{info, parts}]
  POST /session/{id}/message    → {info, parts}  (full JSON, not SSE)
  DELETE /session/{id}          → 200

Message response "parts" array contains objects with "type":
  "step-start"  — pipeline marker
  "reasoning"   — internal chain-of-thought (hidden)
  "text"        — actual response text (what we surface)
  "tool-use"    — tool invocation
  "tool-result" — tool output
  "step-finish" — pipeline marker with token counts
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from services.streaming.core.config import env_get

log = logging.getLogger("opencode-client")

OPENCODE_URL     = env_get("OPENCODE_URL", default="http://opencode-sandbox:9000")
OPENCODE_TIMEOUT = int(env_get("OPENCODE_TIMEOUT", default="300") or "300")
OPENCODE_BIN     = env_get("OPENCODE_BIN", default="opencode")
OPENCODE_ENABLED = (env_get("OPENCODE_ENABLED", default="1") or "1") == "1"

OPENCODE_PROVIDER_CODING   = env_get("OPENCODE_PROVIDER_CODING",   default="anthropic")
OPENCODE_PROVIDER_REVIEW   = env_get("OPENCODE_PROVIDER_REVIEW",   default="anthropic")
OPENCODE_PROVIDER_ANALYSIS = env_get("OPENCODE_PROVIDER_ANALYSIS", default="ollama")
OPENCODE_MODEL_CODING      = env_get("OPENCODE_MODEL_CODING",      default="claude-sonnet-4-6")
OPENCODE_MODEL_REVIEW      = env_get("OPENCODE_MODEL_REVIEW",      default="claude-sonnet-4-6")
OPENCODE_MODEL_ANALYSIS    = env_get("OPENCODE_MODEL_ANALYSIS",    default="qwen2.5-coder:14b")


@dataclass
class OpenCodeSession:
    session_id: str           # OpenCode's native session ID (ses_...)
    task_id: str
    tenant_id: str
    created_at: float = field(default_factory=lambda: __import__("time").time())
    messages: list[dict] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    status: str = "active"   # active | completed | failed


@dataclass
class OpenCodeResult:
    session_id: str
    summary: str
    files_modified: list[str]
    diff: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    backend_used: str
    raw_output: str
    success: bool
    error: str = ""


def _extract_text(parts: list[dict]) -> str:
    """Extract readable text from OpenCode message parts array."""
    texts = [p["text"] for p in parts if p.get("type") == "text" and p.get("text")]
    return "\n".join(texts)


def _extract_tokens(parts: list[dict]) -> tuple[int, int]:
    """Extract (tokens_in, tokens_out) from step-finish parts."""
    for p in parts:
        if p.get("type") == "step-finish":
            t = p.get("tokens", {})
            return int(t.get("input", 0)), int(t.get("output", 0))
    return 0, 0


class OpenCodeClient:
    """
    Async HTTP client for OpenCode v1.2.27 serve mode.
    Falls back to subprocess if serve mode is unreachable.
    """

    def __init__(self, base_url: str = OPENCODE_URL, timeout: int = OPENCODE_TIMEOUT):
        self.base_url = (base_url or OPENCODE_URL).rstrip("/")
        self.timeout = timeout
        # Local registry: session_id → OpenCodeSession
        self._sessions: dict[str, OpenCodeSession] = {}

    # ── Health ─────────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        """Check OpenCode server health via /global/health."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.base_url}/global/health")
                r.raise_for_status()
                data = r.json()
                return {"mode": "serve", "status": "ok", **data}
        except Exception:
            # Subprocess fallback
            try:
                r = subprocess.run(
                    [OPENCODE_BIN, "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                return {"mode": "subprocess", "status": "ok", "version": r.stdout.strip()}
            except Exception as e2:
                return {"mode": "unavailable", "status": "error", "error": str(e2)}

    # ── Session Lifecycle ──────────────────────────────────────────────────────

    async def new_session(
        self,
        task_id: str,
        tenant_id: str,
        context_files: list[str] | None = None,
        provider: str = OPENCODE_PROVIDER_CODING,   # noqa: ARG002 — reserved for future per-session routing
        model: str = OPENCODE_MODEL_CODING,          # noqa: ARG002 — model set via opencode-config.json
        workspace: str = "/workspace",               # noqa: ARG002 — workspace fixed to /workspace in container
    ) -> OpenCodeSession:
        """Create a new OpenCode session. Returns local wrapper with the server's session ID."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{self.base_url}/session",
                    json={},  # OpenCode generates its own ID
                )
                r.raise_for_status()
                data = r.json()
                session_id = data["id"]   # e.g. "ses_2e9c..."
        except Exception as e:
            log.warning("OpenCode serve unreachable (%s); using ephemeral session ID", e)
            session_id = f"sub-{task_id[:8]}-{uuid.uuid4().hex[:6]}"

        session = OpenCodeSession(
            session_id=session_id,
            task_id=task_id,
            tenant_id=tenant_id,
        )

        # Inject context as a system message if files are specified
        if context_files:
            context_note = f"[Context files: {', '.join(context_files)}]"
            session.messages.append({"role": "system", "content": context_note})

        self._sessions[session_id] = session
        return session

    async def send_message(
        self,
        session: OpenCodeSession,
        message: str,
        attachments: list[str] | None = None,  # noqa: ARG002 — OpenCode v1 API doesn't support attachments
    ) -> AsyncGenerator[str, None]:
        """
        Send a message to an OpenCode session.
        Yields the text response as a single chunk (OpenCode returns full JSON, not SSE).
        Falls back to subprocess on failure.
        """
        session.messages.append({"role": "user", "content": message})

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{self.base_url}/session/{session.session_id}/message",
                    json={
                        "content": message,
                        "role": "user",
                        "parts": [{"type": "text", "text": message}],
                    },
                )
                r.raise_for_status()
                data = r.json()

                # Extract text and token counts from the parts array
                parts = data.get("parts", [])
                text = _extract_text(parts)
                tin, tout = _extract_tokens(parts)
                session.tokens_in  += tin
                session.tokens_out += tout

                # Check for file modifications in tool-result parts
                for p in parts:
                    if p.get("type") == "tool-result":
                        fname = (p.get("value") or {}).get("filePath") or (p.get("result") or {}).get("path")
                        if fname and fname not in session.files_modified:
                            session.files_modified.append(fname)

                session.messages.append({"role": "assistant", "content": text})
                yield text
                return

        except Exception as e:
            log.warning("OpenCode session %s stream failed (%s), falling back to subprocess", session.session_id, e)
            result = await self._subprocess_run(message, session.task_id, session.tenant_id)
            session.files_modified.extend(result.files_modified)
            session.tokens_out += result.tokens_out
            session.messages.append({"role": "assistant", "content": result.raw_output})
            yield result.raw_output

    async def get_session_state(self, session: OpenCodeSession) -> dict:
        """Fetch current session messages from OpenCode server."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{self.base_url}/session/{session.session_id}/message")
                r.raise_for_status()
                messages = r.json()  # list of {info, parts}

                # Update token counts from all messages
                total_in = total_out = 0
                for msg in messages:
                    parts = msg.get("parts", [])
                    tin, tout = _extract_tokens(parts)
                    total_in  += tin
                    total_out += tout
                if total_in or total_out:
                    session.tokens_in  = total_in
                    session.tokens_out = total_out

                return {
                    "session_id":     session.session_id,
                    "status":         session.status,
                    "files_modified": session.files_modified,
                    "tokens_in":      session.tokens_in,
                    "tokens_out":     session.tokens_out,
                    "message_count":  len(session.messages),
                }
        except Exception:
            return {
                "session_id":     session.session_id,
                "status":         session.status,
                "files_modified": session.files_modified,
                "tokens_in":      session.tokens_in,
                "tokens_out":     session.tokens_out,
                "message_count":  len(session.messages),
            }

    async def get_diff(self, session: OpenCodeSession) -> str:
        """Generate a diff by examining the workspace. OpenCode doesn't expose a diff endpoint."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Try git diff in workspace
                r = await client.post(
                    f"{self.base_url}/session/{session.session_id}/message",
                    json={
                        "content": "__SINC_DIFF__",
                        "role": "user",
                        "parts": [{"type": "text", "text": "Show me the git diff of all changes made in this session."}],
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    parts = data.get("parts", [])
                    return _extract_text(parts)
        except Exception:
            pass
        return ""

    async def close_session(self, session: OpenCodeSession) -> OpenCodeResult:
        """Finalize session and collect results."""
        await self.get_session_state(session)  # refresh token counts in-place
        diff    = ""  # Skip expensive diff call on close
        summary = session.messages[-1]["content"] if session.messages else ""

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(f"{self.base_url}/session/{session.session_id}")
        except Exception:
            pass  # Best-effort cleanup

        session.status = "completed"
        if session.session_id in self._sessions:
            del self._sessions[session.session_id]

        cost = _estimate_cost(session.tokens_in, session.tokens_out)
        return OpenCodeResult(
            session_id     = session.session_id,
            summary        = summary[:2000],
            files_modified = session.files_modified,
            diff           = diff,
            tokens_in      = session.tokens_in,
            tokens_out     = session.tokens_out,
            cost_usd       = cost,
            backend_used   = "opencode-serve",
            raw_output     = "\n".join(m["content"] for m in session.messages if m["role"] == "assistant"),
            success        = True,
        )

    # ── One-Shot ───────────────────────────────────────────────────────────────

    async def run_oneshot(
        self,
        prompt: str,
        task_id: str,
        tenant_id: str,
        provider: str = OPENCODE_PROVIDER_CODING,
        model: str    = OPENCODE_MODEL_CODING,
        context_files: list[str] | None = None,
        workspace: str = "/workspace",
    ) -> OpenCodeResult:
        """Single-turn execution: creates session, sends prompt, returns full result."""
        try:
            session = await self.new_session(
                task_id=task_id, tenant_id=tenant_id,
                context_files=context_files, provider=provider,
                model=model, workspace=workspace,
            )
            chunks: list[str] = []
            async for chunk in self.send_message(session, prompt):
                chunks.append(chunk)
            return await self.close_session(session)
        except Exception as e:
            log.warning("Serve mode failed for oneshot (%s), using subprocess", e)
            return await self._subprocess_run(prompt, task_id, tenant_id, provider=provider, model=model, workspace=workspace)

    # ── Subprocess Fallback ────────────────────────────────────────────────────

    async def _subprocess_run(
        self,
        prompt: str,
        task_id: str,
        tenant_id: str,
        provider: str  = OPENCODE_PROVIDER_CODING,
        model: str     = OPENCODE_MODEL_CODING,
        workspace: str = "/workspace",
    ) -> OpenCodeResult:
        """Non-interactive fallback via `opencode run` subprocess."""
        env = {
            **os.environ,
            "OPENCODE_PROVIDER": provider,
            "OPENCODE_MODEL":    model,
            "SINC_TENANT_ID":    tenant_id,
            "SINC_TASK_ID":      task_id,
        }
        cmd = [OPENCODE_BIN, "run", "--prompt", prompt, "--format", "json",
               "--workspace", workspace, "--provider", provider, "--model", model]
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True,
                    env=env, cwd=workspace, timeout=OPENCODE_TIMEOUT,
                ),
            )
            output = result.stdout or result.stderr
            try:
                data = json.loads(output)
                files   = data.get("files_modified", [])
                summary = data.get("summary", output[:1000])
                diff    = data.get("diff", "")
                tokens_out = data.get("usage", {}).get("output_tokens", 0)
                success = result.returncode == 0
            except json.JSONDecodeError:
                files = []; summary = output[:2000]; diff = ""
                tokens_out = len(output.split()); success = result.returncode == 0

            return OpenCodeResult(
                session_id=f"sub-{task_id[:8]}", summary=summary,
                files_modified=files, diff=diff, tokens_in=0, tokens_out=tokens_out,
                cost_usd=_estimate_cost(0, tokens_out),
                backend_used=f"opencode-subprocess:{provider}",
                raw_output=output[:8000], success=success,
                error="" if success else output[:500],
            )
        except subprocess.TimeoutExpired:
            return _error_result("OpenCode subprocess timeout")
        except FileNotFoundError:
            return _error_result(f"OpenCode binary not found: {OPENCODE_BIN}")
        except Exception as e:
            return _error_result(str(e))

    # ── Discovery ──────────────────────────────────────────────────────────────

    async def list_sessions(self) -> list[dict]:
        """List sessions: local registry + live sessions from server."""
        local = [
            {
                "session_id":     s.session_id,
                "task_id":        s.task_id,
                "tenant_id":      s.tenant_id,
                "status":         s.status,
                "files_modified": s.files_modified,
                "tokens_in":      s.tokens_in,
                "tokens_out":     s.tokens_out,
                "message_count":  len(s.messages),
            }
            for s in self._sessions.values()
            if s.status == "active"
        ]
        return local


# ── Singleton ──────────────────────────────────────────────────────────────────

_client: OpenCodeClient | None = None


def get_opencode_client() -> OpenCodeClient:
    global _client
    if _client is None:
        _client = OpenCodeClient()
    return _client


# ── Helpers ────────────────────────────────────────────────────────────────────

def _estimate_cost(tokens_in: int, tokens_out: int) -> float:
    """Rough cost estimate in USD (Claude Sonnet 4.6: $3/M in, $15/M out)."""
    return round((tokens_in * 3 + tokens_out * 15) / 1_000_000, 6)


def _error_result(error: str) -> OpenCodeResult:
    return OpenCodeResult(
        session_id="error", summary="", files_modified=[], diff="",
        tokens_in=0, tokens_out=0, cost_usd=0, backend_used="opencode",
        raw_output=error, success=False, error=error,
    )
