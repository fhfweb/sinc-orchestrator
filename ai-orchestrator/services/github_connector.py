from services.streaming.core.config import env_get
"""
GitHub Connector
================
The product gateway.  One API call turns a GitHub repo into a fully-analysed
project in the Orchestrator — Digital Twin, Entropy Dashboard, initial tasks.

Two main capabilities:

1. POST /connect/github  — repo onboarding pipeline
   ┌─────────────────────────────────────────────────┐
   │  clone repo → detect stack → build Digital Twin │
   │  → entropy scan → seed tasks → snapshot v1      │
   └─────────────────────────────────────────────────┘
   Returns a job_id.  Progress tracked via GET /connect/jobs/<id>.

2. POST /webhooks/github  — PR Risk Check
   GitHub sends pull_request events here.
   The connector calls TimeMachine.simulate_change() on the changed files
   and posts a risk-score comment back to the PR.

All GitHub API calls use urllib (no extra deps).
Git clone uses subprocess (git must be in PATH).

Usage:
    from services.github_connector import GitHubConnector
    gc = GitHubConnector(clone_root="/tmp/orch-repos", db_conn_fn=get_db)

    job = gc.connect(
        repo_url="https://github.com/myorg/myrepo",
        access_token="ghp_...",
        project_id="myrepo",
        tenant_id="tenant1",
    )
    # job["job_id"] — poll GET /connect/jobs/<id> for progress
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

CLONE_ROOT = env_get("GITHUB_CLONE_ROOT", default="/tmp/orch-repos")
GITHUB_API = "https://api.github.com"

# Stack detection: extension → language label
_STACK_EXTS: dict[str, str] = {
    ".php": "PHP/Laravel", ".py": "Python", ".ts": "TypeScript",
    ".js": "JavaScript", ".go": "Go", ".java": "Java",
    ".rb": "Ruby", ".cs": "C#", ".rs": "Rust", ".kt": "Kotlin",
}

# Framework fingerprints: filename → label
_FRAMEWORK_FILES: list[tuple[str, str]] = [
    ("artisan",             "Laravel"),
    ("composer.json",       "PHP/Composer"),
    ("package.json",        "Node.js"),
    ("requirements.txt",    "Python"),
    ("pyproject.toml",      "Python"),
    ("go.mod",              "Go"),
    ("Gemfile",             "Ruby on Rails"),
    ("pom.xml",             "Java/Maven"),
    ("build.gradle",        "Java/Gradle"),
    ("Cargo.toml",          "Rust"),
    ("docker-compose.yml",  "Docker"),
    (".github/workflows",   "GitHub Actions"),
]

# Pipeline step names (for progress tracking)
STEPS = [
    "clone",
    "detect_stack",
    "build_twin",
    "entropy_scan",
    "seed_tasks",
    "snapshot",
    "done",
]


# ──────────────────────────────────────────────
# IN-MEMORY JOB STORE
# (Replace with DB-backed store for production)
# ──────────────────────────────────────────────

_jobs: dict[str, dict] = {}   # job_id → job record
_jobs_lock = threading.Lock()


def _new_job(job_id: str, repo_url: str, project_id: str, tenant_id: str) -> dict:
    job = {
        "job_id":     job_id,
        "repo_url":   repo_url,
        "project_id": project_id,
        "tenant_id":  tenant_id,
        "status":     "running",
        "step":       "clone",
        "steps_done": [],
        "steps_log":  {},   # step → {status, message, duration_ms}
        "result":     None,
        "error":      None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _step_start(job: dict, step: str):
    job["step"] = step
    job["steps_log"][step] = {"status": "running", "started_ms": int(time.time() * 1000)}


def _step_done(job: dict, step: str, message: str = ""):
    log = job["steps_log"].get(step, {})
    elapsed = int(time.time() * 1000) - log.get("started_ms", int(time.time() * 1000))
    job["steps_log"][step] = {
        "status":      "done",
        "message":     message,
        "duration_ms": elapsed,
    }
    job["steps_done"].append(step)


def _step_fail(job: dict, step: str, error: str):
    job["steps_log"][step] = {"status": "error", "error": error}
    job["status"] = "error"
    job["error"]  = f"[{step}] {error}"
    job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────
# GIT HELPERS
# ──────────────────────────────────────────────

def _clone(repo_url: str, clone_path: str, access_token: str = "",
           branch: str = "main", depth: int = 1) -> tuple[bool, str]:
    """
    Shallow-clone repo_url to clone_path.
    Injects access_token into HTTPS URL if provided.
    Returns (success, error_message).
    """
    # Inject token into URL for private repos
    if access_token and repo_url.startswith("https://"):
        # https://token@github.com/org/repo
        url = repo_url.replace("https://", f"https://{access_token}@")
    else:
        url = repo_url

    if os.path.exists(clone_path):
        shutil.rmtree(clone_path, ignore_errors=True)
    os.makedirs(os.path.dirname(clone_path), exist_ok=True)

    cmd = [
        "git", "clone",
        "--depth", str(depth),
        "--branch", branch,
        "--single-branch",
        "--no-tags",
        url, clone_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            # Try without branch (might be 'master' or default)
            cmd2 = [
                "git", "clone",
                "--depth", str(depth),
                "--single-branch",
                "--no-tags",
                url, clone_path,
            ]
            result2 = subprocess.run(
                cmd2, capture_output=True, text=True, timeout=300
            )
            if result2.returncode != 0:
                return False, result2.stderr.strip() or result.stderr.strip()
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "git clone timed out (> 5 minutes)"
    except FileNotFoundError:
        return False, "git not found in PATH"
    except Exception as e:
        return False, str(e)


def _pull(clone_path: str, access_token: str = "") -> tuple[bool, str]:
    """Pull latest changes into an existing clone."""
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            capture_output=True, text=True, cwd=clone_path, timeout=120
        )
        return result.returncode == 0, result.stderr.strip()
    except Exception as e:
        return False, str(e)


def _get_default_branch(repo_url: str, access_token: str = "") -> str:
    """Fetch default branch name via GitHub API."""
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", repo_url)
    if not match:
        return "main"
    owner, repo = match.group(1), match.group(2).rstrip(".git")
    headers = {"Accept": "application/vnd.github+json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        req = urllib.request.Request(
            f"{GITHUB_API}/repos/{owner}/{repo}",
            headers=headers
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("default_branch", "main")
    except Exception:
        return "main"


# ──────────────────────────────────────────────
# STACK DETECTION
# ──────────────────────────────────────────────

def _detect_stack(clone_path: str) -> dict:
    """
    Walk top-level directory to detect languages and frameworks.
    Returns { "languages": [...], "frameworks": [...], "primary": "..." }
    """
    lang_counts: dict[str, int] = {}
    frameworks: list[str] = []

    # Check framework fingerprint files
    for fname, label in _FRAMEWORK_FILES:
        check_path = os.path.join(clone_path, fname)
        if os.path.exists(check_path):
            frameworks.append(label)

    # Count source file extensions (top 3 dirs deep)
    for dirpath, dirnames, filenames in os.walk(clone_path):
        # Limit depth to avoid scanning nested vendor dirs
        depth = dirpath[len(clone_path):].count(os.sep)
        if depth > 3:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in {"vendor", "node_modules", ".git",
                                    "storage", "__pycache__", ".venv"}]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            lang = _STACK_EXTS.get(ext)
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

    # Sort by count
    sorted_langs = sorted(lang_counts.items(), key=lambda x: x[1], reverse=True)
    primary = sorted_langs[0][0] if sorted_langs else "Unknown"

    return {
        "languages":  [l for l, _ in sorted_langs[:5]],
        "frameworks": list(set(frameworks)),
        "primary":    primary,
        "file_counts": dict(sorted_langs[:10]),
    }


# ──────────────────────────────────────────────
# GITHUB API CLIENT
# ──────────────────────────────────────────────

class GitHubAPI:
    """Minimal GitHub REST API client (urllib only)."""

    def __init__(self, access_token: str = ""):
        self.token = access_token

    def _headers(self) -> dict:
        h = {
            "Accept":     "application/vnd.github+json",
            "User-Agent": "SINC-Orchestrator/1.0",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = path if path.startswith("http") else f"{GITHUB_API}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data, method=method, headers=self._headers()
        )
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} → {e.code}: {body_text}") from e

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """Return list of changed file paths in a PR."""
        try:
            result = self._req("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
            return [f["filename"] for f in result if isinstance(f, dict)]
        except Exception:
            return []

    def post_pr_comment(self, owner: str, repo: str, pr_number: int,
                        body_md: str) -> bool:
        """Post a markdown comment to a PR. Returns True on success."""
        try:
            self._req("POST", f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
                      {"body": body_md})
            return True
        except Exception:
            return False

    def create_check_run(self, owner: str, repo: str, head_sha: str,
                         name: str, conclusion: str, summary: str,
                         details_url: str = "") -> bool:
        """Create a GitHub Check Run (requires checks:write permission)."""
        try:
            self._req("POST", f"/repos/{owner}/{repo}/check-runs", {
                "name":        name,
                "head_sha":    head_sha,
                "status":      "completed",
                "conclusion":  conclusion,  # success | neutral | failure
                "output": {
                    "title":   name,
                    "summary": summary,
                },
                "details_url": details_url,
            })
            return True
        except Exception:
            return False

    @staticmethod
    def verify_webhook_signature(payload: bytes, secret: str,
                                  signature_header: str) -> bool:
        """Verify GitHub webhook HMAC-SHA256 signature."""
        if not secret or not signature_header:
            return True   # no secret configured → allow all
        expected = "sha256=" + hmac.new(
            secret.encode(), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header)


# ──────────────────────────────────────────────
# PR RISK COMMENT FORMATTER
# ──────────────────────────────────────────────

def _format_pr_comment(result: dict, repo_url: str, dashboard_url: str = "") -> str:
    """
    Format a markdown PR comment from a Time Machine simulation result.
    """
    risk      = result.get("risk_score", 0)
    label     = result.get("risk_label", "unknown").upper()
    rec       = result.get("recommendation", "")
    blast     = result.get("blast_radius", {})
    pre_tasks = result.get("pre_tasks", [])
    broken    = result.get("broken_interfaces", [])

    emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🟠", "CRITICAL": "🔴"}.get(label, "⚪")

    affected = blast.get("affected_files", [])[:8]
    tests    = blast.get("affected_tests",  [])[:5]

    lines = [
        "## 🤖 Orchestrator Risk Analysis",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Risk Score | {emoji} **{label}** ({risk:.2f}) |",
        f"| Recommendation | `{rec}` |",
        f"| Affected Files | {blast.get('files', 0)} |",
        f"| Affected Tests | {blast.get('tests', 0)} |",
        f"| Entropy Delta | +{result.get('entropy_delta', 0):.3f} |",
        "",
    ]

    if broken:
        lines.append("### ⚠️ Potential Interface Breaks")
        for b in broken[:5]:
            lines.append(f"- `{b.get('symbol')}` referenced in `{b.get('caller_file')}`")
        lines.append("")

    if affected:
        lines.append("### 📦 Transitively Affected Modules")
        for f in affected:
            lines.append(f"- `{f}`")
        lines.append("")

    if tests:
        lines.append("### 🧪 Tests That May Need Updates")
        for t in tests:
            lines.append(f"- `{t}`")
        lines.append("")

    if pre_tasks:
        lines.append("### 📋 Suggested Pre-Tasks")
        for pt in pre_tasks[:4]:
            lines.append(f"- **[{pt.get('type','task')}]** {pt.get('title','')}")
        lines.append("")

    if rec == "execute_now":
        lines.append("> ✅ Low impact — safe to merge after review.")
    elif rec == "proceed_with_tests":
        lines.append("> ⚠️ Medium impact — ensure tests pass before merging.")
    elif rec == "split_task":
        lines.append("> 🔧 High impact — consider splitting this PR into smaller changes.")
    else:
        lines.append("> 🚨 Critical impact — manual architecture review recommended before merge.")

    if dashboard_url:
        lines.append(f"\n[View Entropy Dashboard]({dashboard_url})")

    lines.append("\n---\n*[Orchestrator](https://github.com/anthropics) "
                 "• Autonomous Engineering Platform*")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# MAIN CONNECTOR CLASS
# ──────────────────────────────────────────────

class GitHubConnector:
    """
    Orchestrator ↔ GitHub integration.
    Manages repo clones, analysis pipelines, and PR checks.
    """

    def __init__(
        self,
        clone_root:    str = CLONE_ROOT,
        db_conn_fn:    Optional[Callable] = None,
        dashboard_url: str = "",
    ):
        self.clone_root    = clone_root
        self._db           = db_conn_fn
        self.dashboard_url = dashboard_url
        os.makedirs(clone_root, exist_ok=True)

    # ──────────────────────────────────────────
    # REPO CONNECT  (the main pipeline)
    # ──────────────────────────────────────────

    def connect(
        self,
        repo_url:     str,
        access_token: str = "",
        project_id:   str = "",
        tenant_id:    str = "",
        branch:       str = "",
        webhook_secret: str = "",
    ) -> dict:
        """
        Start the onboarding pipeline asynchronously.
        Returns {"job_id": ..., "status": "running"} immediately.
        Progress tracked via get_job(job_id).
        """
        if not project_id:
            # Derive project_id from repo name
            project_id = re.sub(r"[^a-z0-9_-]", "-",
                                 repo_url.rstrip("/").split("/")[-1].lower()
                                 .replace(".git", ""))

        job_id = hashlib.md5(
            f"{repo_url}{tenant_id}{time.time()}".encode()
        ).hexdigest()[:12]

        job = _new_job(job_id, repo_url, project_id, tenant_id)

        # Persist to DB immediately
        self._save_repo(repo_url, project_id, tenant_id,
                        branch or "main", webhook_secret, job_id)

        # Run pipeline in background thread
        t = threading.Thread(
            target=self._run_pipeline,
            args=(job, repo_url, access_token, project_id, tenant_id,
                  branch, webhook_secret),
            daemon=True,
        )
        t.start()

        return {"job_id": job_id, "status": "running",
                "status_url": f"/connect/jobs/{job_id}"}

    def get_job(self, job_id: str) -> Optional[dict]:
        with _jobs_lock:
            return _jobs.get(job_id)

    def list_jobs(self, tenant_id: str = "") -> list[dict]:
        with _jobs_lock:
            jobs = list(_jobs.values())
        if tenant_id:
            jobs = [j for j in jobs if j.get("tenant_id") == tenant_id]
        return sorted(jobs, key=lambda j: j["started_at"], reverse=True)

    # ──────────────────────────────────────────
    # SYNC  (re-scan an already-connected repo)
    # ──────────────────────────────────────────

    def sync(self, project_id: str, tenant_id: str) -> dict:
        """Pull latest changes and re-run the analysis pipeline."""
        repo = self._load_repo(project_id, tenant_id)
        if not repo:
            return {"error": f"project {project_id} not connected"}

        clone_path = repo.get("clone_path", "")
        if not clone_path or not os.path.isdir(clone_path):
            # Need to re-clone
            return self.connect(
                repo["repo_url"], repo.get("access_token", ""),
                project_id, tenant_id, repo.get("branch", "main")
            )

        success, err = _pull(clone_path)
        if not success:
            return {"error": f"git pull failed: {err}"}

        job_id = hashlib.md5(f"sync-{project_id}{time.time()}".encode()).hexdigest()[:12]
        job    = _new_job(job_id, repo["repo_url"], project_id, tenant_id)
        job["step"] = "detect_stack"   # skip clone step

        t = threading.Thread(
            target=self._run_analysis,
            args=(job, clone_path, project_id, tenant_id),
            daemon=True,
        )
        t.start()
        return {"job_id": job_id, "status": "running",
                "status_url": f"/connect/jobs/{job_id}"}

    # ──────────────────────────────────────────
    # PR RISK CHECK
    # ──────────────────────────────────────────

    def handle_pr_event(
        self,
        payload:       dict,
        access_token:  str = "",
        project_id:    str = "",
        project_path:  str = "",
        tenant_id:     str = "",
    ) -> dict:
        """
        Process a GitHub pull_request webhook event.
        Runs Time Machine simulation on changed files and posts a PR comment.
        Returns the simulation result.
        """
        action = payload.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            return {"skipped": True, "reason": f"action={action}"}

        pr   = payload.get("pull_request", {})
        repo = payload.get("repository", {})

        pr_number = pr.get("number")
        head_sha  = pr.get("head", {}).get("sha", "")
        owner     = repo.get("owner", {}).get("login", "")
        repo_name = repo.get("name", "")
        pr_title  = pr.get("title", "")

        if not pr_number or not owner or not repo_name:
            return {"error": "invalid PR payload"}

        # Fetch changed files via GitHub API
        gh    = GitHubAPI(access_token)
        files = gh.get_pr_files(owner, repo_name, pr_number)

        if not files:
            return {"skipped": True, "reason": "no changed files found"}

        # Simulation
        result: dict = {}
        if project_path and os.path.isdir(project_path):
            try:
                from services.time_machine import TimeMachine
                tm = TimeMachine(db_conn_fn=self._db)
                change_spec = {
                    "files_modified": files,
                    "task_title": pr_title or f"PR #{pr_number}",
                    "task_id":    f"PR-{owner}-{repo_name}-{pr_number}",
                }
                result = tm.simulate_change(
                    change_spec, project_path, project_id, tenant_id
                )
            except ImportError:
                result = {
                    "warning": "time_machine unavailable",
                    "blast_radius": {"files": len(files), "tests": 0,
                                     "affected_files": files, "affected_tests": []},
                    "risk_score":     0.0,
                    "risk_label":     "unknown",
                    "recommendation": "proceed_with_tests",
                    "broken_interfaces": [],
                    "entropy_delta": 0.0,
                    "pre_tasks": [],
                }
        else:
            # Lightweight blast-radius-only mode (no local clone needed)
            result = {
                "blast_radius": {"files": len(files), "tests": 0,
                                 "affected_files": files, "affected_tests": []},
                "risk_score":     0.0,
                "risk_label":     "unknown",
                "recommendation": "proceed_with_tests",
                "broken_interfaces": [],
                "entropy_delta": 0.0,
                "pre_tasks":    [],
            }

        # Format and post comment
        dash_url = (f"{self.dashboard_url}/entropy/dashboard?project_id={project_id}"
                    if self.dashboard_url else "")
        comment  = _format_pr_comment(result, f"https://github.com/{owner}/{repo_name}",
                                       dash_url)
        gh.post_pr_comment(owner, repo_name, pr_number, comment)

        # Optionally create a Check Run
        conclusion = {
            "low":      "success",
            "medium":   "neutral",
            "high":     "failure",
            "critical": "failure",
            "unknown":  "neutral",
        }.get(result.get("risk_label", "unknown"), "neutral")

        gh.create_check_run(
            owner, repo_name, head_sha,
            name="Orchestrator Risk Analysis",
            conclusion=conclusion,
            summary=result.get("summary", ""),
            details_url=dash_url,
        )

        return {"pr_number": pr_number, "files_changed": len(files), **result}

    # ──────────────────────────────────────────
    # CONNECTED REPOS LISTING
    # ──────────────────────────────────────────

    def list_repos(self, tenant_id: str) -> list[dict]:
        """Return all connected repos for a tenant."""
        if not self._db:
            return []
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, repo_url, project_id, branch,
                               stack, sync_status, connected_at, last_sync_at
                        FROM connected_repos
                        WHERE tenant_id = %s
                        ORDER BY connected_at DESC
                    """, (tenant_id,))
                    return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    # ──────────────────────────────────────────
    # INTERNAL PIPELINE
    # ──────────────────────────────────────────

    def _run_pipeline(self, job: dict, repo_url: str, access_token: str,
                      project_id: str, tenant_id: str,
                      branch: str, webhook_secret: str):
        """Full pipeline: clone + analysis."""
        # Resolve default branch if not specified
        if not branch:
            branch = _get_default_branch(repo_url, access_token) or "main"

        clone_path = os.path.join(self.clone_root, tenant_id, project_id)

        # Step 1: Clone
        _step_start(job, "clone")
        ok, err = _clone(repo_url, clone_path, access_token, branch)
        if not ok:
            _step_fail(job, "clone", err)
            return
        _step_done(job, "clone", f"cloned to {clone_path}")

        # Update DB with clone path
        self._update_repo_clone_path(project_id, tenant_id, clone_path)

        # Continue with analysis
        self._run_analysis(job, clone_path, project_id, tenant_id)

    def _run_analysis(self, job: dict, clone_path: str,
                      project_id: str, tenant_id: str):
        """Analysis pipeline (used by both connect and sync)."""
        result: dict = {
            "project_id":  project_id,
            "clone_path":  clone_path,
        }

        # Step 2: Detect stack
        _step_start(job, "detect_stack")
        try:
            stack = _detect_stack(clone_path)
            result["stack"] = stack
            self._update_repo_stack(project_id, tenant_id, stack)
            _step_done(job, "detect_stack",
                       f"primary: {stack['primary']}, "
                       f"frameworks: {', '.join(stack['frameworks'][:3])}")
        except Exception as e:
            _step_fail(job, "detect_stack", str(e))
            return

        # Step 3: Build Digital Twin
        _step_start(job, "build_twin")
        twin_stats: dict = {}
        try:
            from services.digital_twin import DigitalTwin
            twin = DigitalTwin()
            twin_stats = twin.sync_project(
                clone_path, project_id, tenant_id,
                on_progress=None,
            )
            result["twin"] = twin_stats
            _step_done(job, "build_twin",
                       f"{twin_stats.get('files', 0)} files, "
                       f"{twin_stats.get('nodes_created', 0)} nodes, "
                       f"{twin_stats.get('infra_services', 0)} services")
        except ImportError:
            _step_done(job, "build_twin", "digital_twin module unavailable — skipped")
        except Exception as e:
            _step_done(job, "build_twin", f"partial: {e}")

        # Step 4: Entropy scan
        _step_start(job, "entropy_scan")
        entropy_summary: dict = {}
        try:
            from services.entropy_scanner import EntropyScanner
            scanner = EntropyScanner(db_conn_fn=self._db)
            entropy_summary = scanner.scan_and_store(clone_path, project_id, tenant_id)
            result["entropy"] = entropy_summary
            _step_done(
                job, "entropy_scan",
                f"{entropy_summary.get('files_scanned', 0)} files — "
                f"critical: {entropy_summary.get('critical', 0)}, "
                f"refactor: {entropy_summary.get('refactor', 0)}, "
                f"avg entropy: {entropy_summary.get('avg_entropy', 0):.2f}"
            )
        except ImportError:
            _step_done(job, "entropy_scan", "entropy_scanner unavailable — skipped")
        except Exception as e:
            _step_done(job, "entropy_scan", f"partial: {e}")

        # Step 5: Seed initial tasks from entropy
        _step_start(job, "seed_tasks")
        seeded_tasks: list = []
        try:
            from services.entropy_scanner import EntropyScanner
            scanner = EntropyScanner(db_conn_fn=self._db)
            seeded_tasks = scanner.seed_tasks(
                project_id, tenant_id, threshold=0.65
            )
            result["seeded_tasks"] = seeded_tasks
            _step_done(job, "seed_tasks",
                       f"{len(seeded_tasks)} repair tasks created")
        except Exception as e:
            _step_done(job, "seed_tasks", f"partial: {e}")

        # Step 6: Save snapshot metadata to DB
        _step_start(job, "snapshot")
        try:
            snapshot_id = self._save_snapshot(
                project_id, tenant_id, clone_path, twin_stats, entropy_summary
            )
            result["snapshot_id"] = snapshot_id
            _step_done(job, "snapshot", f"snapshot_id={snapshot_id}")
        except Exception as e:
            _step_done(job, "snapshot", f"partial: {e}")

        # Done
        job["step"]        = "done"
        job["status"]      = "done"
        job["result"]      = result
        job["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if "done" not in job["steps_done"]:
            job["steps_done"].append("done")

        # Update DB sync status
        self._update_repo_sync_status(project_id, tenant_id, "done")

    # ──────────────────────────────────────────
    # DB PERSISTENCE HELPERS
    # ──────────────────────────────────────────

    def _save_repo(self, repo_url: str, project_id: str, tenant_id: str,
                   branch: str, webhook_secret: str, job_id: str):
        if not self._db:
            return
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO connected_repos
                            (id, repo_url, project_id, tenant_id, branch,
                             webhook_secret, sync_status, connected_at)
                        VALUES (%s,%s,%s,%s,%s,%s,'connecting', NOW())
                        ON CONFLICT (id) DO UPDATE
                        SET sync_status = 'connecting', repo_url = EXCLUDED.repo_url
                    """, (
                        hashlib.md5(f"{tenant_id}:{project_id}".encode()).hexdigest()[:16],
                        repo_url, project_id, tenant_id, branch,
                        webhook_secret,
                    ))
                    conn.commit()
        except Exception:
            pass

    def _update_repo_clone_path(self, project_id: str, tenant_id: str, path: str):
        if not self._db:
            return
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE connected_repos SET clone_path = %s "
                        "WHERE project_id = %s AND tenant_id = %s",
                        (path, project_id, tenant_id)
                    )
                    conn.commit()
        except Exception:
            pass

    def _update_repo_stack(self, project_id: str, tenant_id: str, stack: dict):
        if not self._db:
            return
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE connected_repos SET stack = %s "
                        "WHERE project_id = %s AND tenant_id = %s",
                        (json.dumps(stack), project_id, tenant_id)
                    )
                    conn.commit()
        except Exception:
            pass

    def _update_repo_sync_status(self, project_id: str, tenant_id: str, status: str):
        if not self._db:
            return
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE connected_repos "
                        "SET sync_status = %s, last_sync_at = NOW() "
                        "WHERE project_id = %s AND tenant_id = %s",
                        (status, project_id, tenant_id)
                    )
                    conn.commit()
        except Exception:
            pass

    def _load_repo(self, project_id: str, tenant_id: str) -> Optional[dict]:
        if not self._db:
            return None
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM connected_repos "
                        "WHERE project_id = %s AND tenant_id = %s",
                        (project_id, tenant_id)
                    )
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception:
            return None

    def _save_snapshot(self, project_id: str, tenant_id: str,
                       clone_path: str, twin_stats: dict,
                       entropy_summary: dict) -> str:
        snapshot_id = hashlib.md5(
            f"{project_id}{tenant_id}{time.time()}".encode()
        ).hexdigest()[:16]
        if not self._db:
            return snapshot_id
        try:
            with self._db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO repo_snapshots
                            (id, project_id, tenant_id, clone_path,
                             twin_stats, entropy_summary, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s, NOW())
                    """, (
                        snapshot_id, project_id, tenant_id, clone_path,
                        json.dumps(twin_stats), json.dumps(entropy_summary),
                    ))
                    conn.commit()
        except Exception:
            pass
        return snapshot_id


# ──────────────────────────────────────────────
# MODULE-LEVEL SINGLETON
# ──────────────────────────────────────────────

_connector: Optional[GitHubConnector] = None


def get_connector(db_conn_fn: Optional[Callable] = None,
                  dashboard_url: str = "") -> GitHubConnector:
    global _connector
    if _connector is None:
        _connector = GitHubConnector(
            clone_root=CLONE_ROOT,
            db_conn_fn=db_conn_fn,
            dashboard_url=dashboard_url,
        )
    return _connector


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "connect":
        if len(sys.argv) < 3:
            print("Usage: github_connector.py connect <repo_url> [project_id] [branch]")
            sys.exit(1)
        repo_url     = sys.argv[2]
        project_id   = sys.argv[3] if len(sys.argv) > 3 else ""
        branch       = sys.argv[4] if len(sys.argv) > 4 else ""
        access_token = env_get("GITHUB_TOKEN", default="")
        project_path = env_get("PROJECT_ID", default="default")
        tenant_id    = env_get("TENANT_ID", default="local")

        gc  = GitHubConnector(clone_root="/tmp/orch-repos-cli")
        job = gc.connect(repo_url, access_token, project_id or project_path,
                         tenant_id, branch)

        print(f"Job started: {job['job_id']}")
        # Wait for completion
        while True:
            time.sleep(2)
            j = gc.get_job(job["job_id"])
            if not j:
                break
            step = j.get("step", "?")
            status = j.get("status", "?")
            print(f"  [{status}] {step} ... ", end="", flush=True)
            if status in ("done", "error"):
                print()
                if status == "done":
                    r = j.get("result", {})
                    print(json.dumps({
                        "stack":    r.get("stack", {}).get("primary"),
                        "twin":     r.get("twin", {}),
                        "entropy":  {k: r.get("entropy", {}).get(k)
                                     for k in ["files_scanned","critical","avg_entropy"]},
                        "tasks":    len(r.get("seeded_tasks", [])),
                    }, indent=2))
                else:
                    print(f"ERROR: {j.get('error')}")
                break
            print()

    elif cmd == "detect-stack":
        path = sys.argv[2] if len(sys.argv) > 2 else "."
        print(json.dumps(_detect_stack(path), indent=2))

    else:
        print("Usage: python github_connector.py <cmd>")
        print("  connect <repo_url> [project_id] [branch]")
        print("  detect-stack <path>")
        print()
        print("Env: GITHUB_TOKEN, TENANT_ID, PROJECT_ID")
