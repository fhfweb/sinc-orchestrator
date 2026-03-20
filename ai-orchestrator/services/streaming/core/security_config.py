"""
streaming/core/security_config.py
==================================
Security-related constants and utilities.
Separated from config.py to make security surface area explicit and auditable.
"""
from pathlib import Path
from typing import Optional
from urllib.parse import unquote
from services.streaming.core.config import env_get

# ── Payload size guards ───────────────────────────────────────────────────────
# Maximum bytes allowed in a completion payload summary field.
MAX_COMPLETION_PAYLOAD_BYTES: int = int(
    env_get("MAX_COMPLETION_PAYLOAD_BYTES", default=str(1_000_000))
)

# ── Project path sandbox ──────────────────────────────────────────────────────
# All user-supplied project paths are resolved relative to this base directory.
# Paths that escape the sandbox (path traversal) are rejected with HTTP 400.
BASE_PROJECTS_DIR: str = env_get(
    "AGENT_WORKSPACE",
    default=str(Path(__file__).parent.parent.parent.parent),  # project root fallback
)


def safe_project_path(user_path: str, base_dir: Optional[str] = None) -> str:
    """
    Resolve and validate a user-supplied project path against a base directory.
    Raises ValueError on path-traversal attempts (e.g. '../../etc/passwd').
    Returns the resolved absolute path string.
    """
    workspace = base_dir or BASE_PROJECTS_DIR
    base      = Path(workspace).resolve()
    normalized_user_path = unquote(user_path or "")
    try:
        candidate = (base / normalized_user_path).resolve()
    except Exception:
        raise ValueError(f"Invalid project path: {user_path!r}")
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValueError(f"Path traversal attempt detected: {user_path!r}")
    return str(candidate)
