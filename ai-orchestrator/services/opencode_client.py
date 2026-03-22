"""
opencode_client.py
==================
Async client for the OpenCode AI coding assistant.

Provides:
- Session lifecycle management (create, continue, close)
- Streaming token delivery via async generator
- File diff extraction from sessions
- Non-interactive (one-shot) task execution
- Integration with SINC memory (context injection via Qdrant)
- Health check and capability discovery

Architecture:
  OpenCode runs in serve mode inside a Docker sandbox:
    opencode serve --port 9000 --mcp-url http://sinc-mcp:8000/mcp/

  This client talks to that HTTP API.
  Fallback: subprocess mode via `opencode run --format json`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Any, Optional

import httpx

from services.streaming.core.config import env_get

log = logging.getLogger("opencode-client")

OPENCODE_URL     = env_get("OPENCODE_URL", default="http://opencode-sandbox:9000")
OPENCODE_TIMEOUT = int(env_get("OPENCODE_TIMEOUT", default="300"))
OPENCODE_BIN     = env_get("OPENCODE_BIN", default="opencode")
OPENCODE_ENABLED = env_get("OPENCODE_ENABLED", default="1") == "1"

# Model routing: OpenCode can use different providers per task type
OPENCODE_PROVIDER_CODING   = env_get("OPENCODE_PROVIDER_CODING",   default="anthropic")
OPENCODE_PROVIDER_REVIEW   = env_get("OPENCODE_PROVIDER_REVIEW",   default="anthropic")
OPENCODE_PROVIDER_ANALYSIS = env_get("OPENCODE_PROVIDER_ANALYSIS", default="ollama")
OPENCODE_MODEL_CODING      = env_get("OPENCODE_MODEL_CODING",      default="claude-sonnet-4-6")
OPENCODE_MODEL_REVIEW      = env_get("OPENCODE_MODEL_REVIEW",      default="claude-sonnet-4-6")
OPENCODE_MODEL_ANALYSIS    = env_get("OPENCODE_MODEL_ANALYSIS",    default="qwen2.5-coder:14b")


@dataclass
class OpenCodeSession:
    session_id: str
    task_id: str
    tenant_id: str
    created_at: float = field(default_factory=lambda: __import__("time").time())
    messages: list[dict] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    status: str = "active"  # active | completed | failed


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


class OpenCodeClient:
    """
    Async HTTP client for OpenCode serve mode.
    Falls back to subprocess (one-shot) if serve mode is unavailable.
    """

    def __init__(self, base_url: str = OPENCODE_URL, timeout: int = OPENCODE_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._sessions: dict[str, OpenCodeSession] = {}

    # ── Session Lifecycle ──────────────────────────────────────────────────────

    async def new_session(
        self,
        task_id: str,
        tenant_id: str,
        context_files: list[str] | None = None,
        provider: str = OPENCODE_PROVIDER_CODING,
        model: str = OPENCODE_MODEL_CODING,
        workspace: str = "/workspace",
    ) -> OpenCodeSession:
        """Create a new OpenCode session and inject SINC context."""
        session_id = f"oc-{task_id[:8]}-{uuid.uuid4().hex[:6]}"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{self.base_url}/session",
                    json={
                        "session_id":    session_id,
                        "provider":      provider,
                        "model":         model,
                        "workspace":     workspace,
                        "tenant_id":     tenant_id,
                        "task_id":       task_id,
                        "context_files": context_files or [],
                    },
                )
                r.raise_for_status()
                data = r.json()
                session_id = data.get("session_id", session_id)
        except Exception as e:
            log.warning(f"OpenCode serve mode unavailable ({e}), session will use subprocess fallback")

        session = OpenCodeSession(
            session_id=session_id,
            task_id=task_id,
            tenant_id=tenant_id,
        )
        self._sessions[session_id] = session
        return session

    async def send_message(
        self,
        session: OpenCodeSession,
        message: str,
        attachments: list[str] | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Send a message to an OpenCode session.
        Yields streamed token chunks as they arrive.
        """
        session.messages.append({"role": "user", "content": message})

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/session/{session.session_id}/message",
                    json={
                        "content":     message,
                        "attachments": attachments or [],
                        "tenant_id":   session.tenant_id,
                    },
                ) as response:
                    response.raise_for_status()
                    full_text = []
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("data: "):
                            chunk = line[6:]
                            if chunk == "[DONE]":
                                break
                            try:
                                data = json.loads(chunk)
                                token = data.get("token", "")
                                if token:
                                    full_text.append(token)
                                    yield token
                                # Track usage
                                if data.get("usage"):
                                    session.tokens_in  += data["usage"].get("input_tokens", 0)
                                    session.tokens_out += data["usage"].get("output_tokens", 0)
                                # Track file modifications
                                if data.get("file_modified"):
                                    f = data["file_modified"]
                                    if f not in session.files_modified:
                                        session.files_modified.append(f)
                            except json.JSONDecodeError:
                                yield chunk

                    session.messages.append({"role": "assistant", "content": "".join(full_text)})
                    return

        except Exception as e:
            log.warning(f"Session stream failed ({e}), falling back to subprocess")
            # Subprocess fallback — yields full response as single chunk
            result = await self._subprocess_run(message, session.task_id, session.tenant_id)
            session.files_modified.extend(result.files_modified)
            session.tokens_out += result.tokens_out
            yield result.raw_output

    async def get_session_state(self, session: OpenCodeSession) -> dict:
        """Fetch current session state: files changed, token usage, status."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{self.base_url}/session/{session.session_id}",
                    params={"tenant_id": session.tenant_id},
                )
                r.raise_for_status()
                data = r.json()
                session.files_modified = data.get("files_modified", session.files_modified)
                session.tokens_in      = data.get("tokens_in",      session.tokens_in)
                session.tokens_out     = data.get("tokens_out",     session.tokens_out)
                session.status         = data.get("status",         session.status)
                return data
        except Exception:
            return {"session_id": session.session_id, "status": session.status}

    async def get_diff(self, session: OpenCodeSession) -> str:
        """Get unified diff of all files modified in this session."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{self.base_url}/session/{session.session_id}/diff",
                    params={"tenant_id": session.tenant_id},
                )
                r.raise_for_status()
                return r.json().get("diff", "")
        except Exception:
            return ""

    async def close_session(self, session: OpenCodeSession) -> OpenCodeResult:
        """Finalize session and collect all results."""
        state   = await self.get_session_state(session)
        diff    = await self.get_diff(session)
        summary = session.messages[-1]["content"] if session.messages else ""

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.delete(
                    f"{self.base_url}/session/{session.session_id}",
                    params={"tenant_id": session.tenant_id},
                )
        except Exception:
            pass

        session.status = "completed"
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
            success        = state.get("status") != "failed",
        )

    # ── One-Shot Execution ─────────────────────────────────────────────────────

    async def run_oneshot(
        self,
        prompt: str,
        task_id: str,
        tenant_id: str,
        provider: str = OPENCODE_PROVIDER_CODING,
        model: str = OPENCODE_MODEL_CODING,
        context_files: list[str] | None = None,
        workspace: str = "/workspace",
    ) -> OpenCodeResult:
        """
        Single-turn execution: best for atomic tasks.
        Tries serve mode first, then subprocess.
        """
        # Try serve mode via ephemeral session
        try:
            session = await self.new_session(
                task_id=task_id, tenant_id=tenant_id,
                context_files=context_files, provider=provider,
                model=model, workspace=workspace,
            )
            chunks = []
            async for chunk in self.send_message(session, prompt):
                chunks.append(chunk)
            return await self.close_session(session)
        except Exception as e:
            log.warning(f"Serve mode failed for oneshot ({e}), using subprocess")
            return await self._subprocess_run(prompt, task_id, tenant_id, provider=provider, model=model, workspace=workspace)

    async def _subprocess_run(
        self,
        prompt: str,
        task_id: str,
        tenant_id: str,
        provider: str = OPENCODE_PROVIDER_CODING,
        model: str    = OPENCODE_MODEL_CODING,
        workspace: str = "/workspace",
    ) -> OpenCodeResult:
        """Non-interactive subprocess execution via `opencode run`."""
        env = {
            **os.environ,
            "OPENCODE_PROVIDER": provider,
            "OPENCODE_MODEL":    model,
            "SINC_TENANT_ID":    tenant_id,
            "SINC_TASK_ID":      task_id,
        }
        cmd = [
            OPENCODE_BIN, "run",
            "--prompt",    prompt,
            "--format",    "json",
            "--workspace", workspace,
            "--provider",  provider,
            "--model",     model,
        ]
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
                files = data.get("files_modified", [])
                summary = data.get("summary", output[:1000])
                diff = data.get("diff", "")
                tokens_out = data.get("usage", {}).get("output_tokens", 0)
                success = result.returncode == 0
            except json.JSONDecodeError:
                files = []
                summary = output[:2000]
                diff = ""
                tokens_out = len(output.split())
                success = result.returncode == 0

            return OpenCodeResult(
                session_id     = f"sub-{task_id[:8]}",
                summary        = summary,
                files_modified = files,
                diff           = diff,
                tokens_in      = 0,
                tokens_out     = tokens_out,
                cost_usd       = _estimate_cost(0, tokens_out),
                backend_used   = f"opencode-subprocess:{provider}",
                raw_output     = output[:8000],
                success        = success,
                error          = "" if success else output[:500],
            )
        except subprocess.TimeoutExpired:
            return _error_result(task_id, "OpenCode subprocess timeout")
        except FileNotFoundError:
            return _error_result(task_id, f"OpenCode binary not found: {OPENCODE_BIN}")
        except Exception as e:
            return _error_result(task_id, str(e))

    # ── Health & Discovery ─────────────────────────────────────────────────────

    async def health(self) -> dict:
        """Check OpenCode server health."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{self.base_url}/health")
                r.raise_for_status()
                return {"mode": "serve", "status": "ok", **r.json()}
        except Exception:
            # Try subprocess
            try:
                r = subprocess.run([OPENCODE_BIN, "--version"], capture_output=True, text=True, timeout=5)
                return {"mode": "subprocess", "status": "ok", "version": r.stdout.strip()}
            except Exception as e2:
                return {"mode": "unavailable", "status": "error", "error": str(e2)}

    async def list_sessions(self) -> list[dict]:
        """List active OpenCode sessions."""
        return [
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


# ── Module-level singleton ─────────────────────────────────────────────────────

_client: OpenCodeClient | None = None

def get_opencode_client() -> OpenCodeClient:
    global _client
    if _client is None:
        _client = OpenCodeClient()
    return _client


# ── Helpers ────────────────────────────────────────────────────────────────────

def _estimate_cost(tokens_in: int, tokens_out: int, model: str = OPENCODE_MODEL_CODING) -> float:
    """Rough cost estimate in USD based on Claude Sonnet 4.6 pricing."""
    # $3/M input, $15/M output (Claude Sonnet 4.6)
    return round((tokens_in * 3 + tokens_out * 15) / 1_000_000, 6)

def _error_result(task_id: str, error: str) -> OpenCodeResult:
    return OpenCodeResult(
        session_id="error", summary="", files_modified=[], diff="",
        tokens_in=0, tokens_out=0, cost_usd=0, backend_used="opencode",
        raw_output=error, success=False, error=error,
    )
