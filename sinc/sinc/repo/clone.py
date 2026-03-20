"""Clone a remote repository to a temporary directory.

Uses git (must be on PATH) — zero extra Python dependencies.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile


def _inject_token(repo_url: str, token: str) -> str:
    """Inject a Personal Access Token into an HTTPS GitHub URL."""
    if token and repo_url.startswith("https://"):
        return re.sub(r"https://", f"https://{token}@", repo_url, count=1)
    return repo_url


def clone(
    repo_url: str,
    branch:   str = "",
    token:    str = "",
    depth:    int = 1,
) -> str:
    """Shallow-clone repo_url into a fresh temp directory.

    Returns the path to the cloned repo.
    The *caller* is responsible for cleanup (shutil.rmtree).

    Raises RuntimeError on clone failure or missing git.
    """
    url = _inject_token(repo_url, token)
    tmp = tempfile.mkdtemp(prefix="sinc_")

    cmd = ["git", "clone", "--depth", str(depth)]
    if branch:
        cmd += ["--branch", branch]
    cmd += [url, tmp]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(
            "git not found — please install git: https://git-scm.com/"
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("git clone timed out after 5 minutes")

    if result.returncode != 0:
        # If branch flag caused the failure, retry without it
        if branch:
            shutil.rmtree(tmp, ignore_errors=True)
            tmp = tempfile.mkdtemp(prefix="sinc_")
            cmd2 = ["git", "clone", "--depth", str(depth), url, tmp]
            try:
                result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("git clone timed out after 5 minutes")

            if result2.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError(
                    f"git clone failed: {result2.stderr.strip() or result.stderr.strip()}"
                )
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

    return tmp


def detect_branch(clone_path: str) -> str:
    """Return the active branch name of a cloned repo."""
    try:
        r = subprocess.run(
            ["git", "-C", clone_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() or "main"
    except Exception:
        return "main"


def repo_name_from_url(repo_url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL."""
    # https://github.com/owner/repo[.git]
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", repo_url.rstrip("/"))
    if m:
        return m.group(1)
    # fallback: last two path segments
    parts = repo_url.rstrip("/").split("/")
    if len(parts) >= 2:
        return "/".join(parts[-2:]).replace(".git", "")
    return repo_url
