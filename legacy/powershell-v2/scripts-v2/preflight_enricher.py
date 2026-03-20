"""
SINC Orchestrator Preflight Enricher v2
Enriches preflight JSON files with:
  1. Top-K semantic context from Qdrant (relevant architecture/code docs)
  2. Lessons learned from knowledge_base/ for similar past tasks
  3. File hashes of files_to_change (detect staleness)

Usage:
    python preflight_enricher.py --task-id FEAT-SINC-AUTH-RBAC-001
    python preflight_enricher.py --all     (enrich all pending task preflights)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE              = Path(__file__).parent.parent.parent
PREFLIGHT_DIR     = BASE / "tasks" / "preflight"
KNOWLEDGE_BASE    = BASE / "knowledge_base"
FILE_HASHES       = BASE / "state" / "file-hashes.json"
TASKS_DAG         = BASE / "tasks" / "task-dag.json"
SINC_APP          = BASE.parent / "app"  # Laravel app root

QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "sinc-knowledge")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11435")
OLLAMA_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "llama3:8b")

TOP_K_CONTEXT = 5
TOP_K_LESSONS = 3


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str):
    print(f"[preflight-enricher] {msg}")


# ─────────────────────────────────────────────
# QDRANT CONTEXT RETRIEVAL
# ─────────────────────────────────────────────

def _get_embedding(text: str) -> Optional[list[float]]:
    """Get embedding from Ollama."""
    try:
        import urllib.request
        import urllib.error
        payload = json.dumps({"model": OLLAMA_MODEL, "prompt": text}).encode()
        req = urllib.request.Request(
            f"{OLLAMA_HOST}/api/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("embedding")
    except Exception as e:
        _log(f"  WARNING: Could not get embedding: {e}")
        return None


def _qdrant_search(query: str, top_k: int = TOP_K_CONTEXT) -> list[dict]:
    """Search Qdrant for semantically similar documents."""
    embedding = _get_embedding(query)
    if not embedding:
        return []
    try:
        import urllib.request
        payload = json.dumps({
            "vector": embedding,
            "limit": top_k,
            "with_payload": True,
        }).encode()
        req = urllib.request.Request(
            f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{QDRANT_COLLECTION}/points/search",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return [
                {
                    "score":   r.get("score", 0),
                    "id":      r.get("id"),
                    "payload": r.get("payload", {}),
                }
                for r in result.get("result", [])
            ]
    except Exception as e:
        _log(f"  WARNING: Qdrant search failed: {e}")
        return []


# ─────────────────────────────────────────────
# LESSONS LEARNED
# ─────────────────────────────────────────────

def _get_relevant_lessons(task_id: str, objective: str) -> list[dict]:
    """Find lessons from knowledge_base/ relevant to this task."""
    lessons = []
    if not KNOWLEDGE_BASE.exists():
        return lessons

    for lesson_file in sorted(KNOWLEDGE_BASE.glob("*.json"))[:50]:
        try:
            data = json.loads(lesson_file.read_text(encoding="utf-8"))
            # Simple keyword matching — Qdrant search is preferred but this works offline
            lesson_text = (
                data.get("title", "") + " " +
                data.get("description", "") + " " +
                data.get("domain", "") + " " +
                " ".join(data.get("tags", []))
            ).lower()

            task_keywords = set(
                (task_id + " " + objective).lower().split()
            ) - {"the", "a", "and", "or", "to", "for", "in", "with", "of"}

            overlap = sum(1 for kw in task_keywords if kw in lesson_text)
            if overlap >= 2:
                lessons.append({
                    "lesson_id": data.get("id", lesson_file.stem),
                    "title":     data.get("title", lesson_file.stem),
                    "summary":   data.get("summary", data.get("description", ""))[:300],
                    "relevance_score": overlap / max(len(task_keywords), 1),
                })
        except Exception:
            continue

    lessons.sort(key=lambda x: x["relevance_score"], reverse=True)
    return lessons[:TOP_K_LESSONS]


# ─────────────────────────────────────────────
# FILE HASH CONTEXT
# ─────────────────────────────────────────────

def _get_file_context(files_to_change: list[str]) -> list[dict]:
    """Get current hash and size for files that will be modified."""
    context = []
    hashes = {}
    if FILE_HASHES.exists():
        try:
            hashes = json.loads(FILE_HASHES.read_text(encoding="utf-8"))
        except Exception:
            pass

    for f in files_to_change[:10]:  # limit to 10 files
        file_info = {"path": f}
        if f in hashes:
            file_info["last_hash"]    = hashes[f].get("hash", "unknown")
            file_info["last_updated"] = hashes[f].get("updated_at", "unknown")

        # Check if file exists in SINC app
        candidate = SINC_APP / f.lstrip("/")
        if candidate.exists():
            file_info["exists"] = True
            file_info["size_bytes"] = candidate.stat().st_size
        else:
            file_info["exists"] = False

        context.append(file_info)
    return context


# ─────────────────────────────────────────────
# ENRICHMENT
# ─────────────────────────────────────────────

def enrich_preflight(preflight_path: Path, dry_run: bool = False) -> bool:
    """Enrich a single preflight file. Returns True if enriched."""
    try:
        data = json.loads(preflight_path.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"  ERROR reading {preflight_path.name}: {e}")
        return False

    # Skip if already enriched
    if data.get("enrichment_version") == "v2":
        _log(f"  SKIP {preflight_path.name} (already enriched)")
        return False

    task_id   = data.get("task_id", "")
    objective = data.get("objective", "")
    query     = f"{task_id} {objective}"

    _log(f"  Enriching {preflight_path.name}...")

    # 1. Semantic context from Qdrant
    qdrant_results = _qdrant_search(query)
    semantic_context = [
        {
            "score":   r["score"],
            "title":   r["payload"].get("title", r["payload"].get("source", "")),
            "excerpt": str(r["payload"].get("content", r["payload"]))[:400],
        }
        for r in qdrant_results
    ]

    # 2. Lessons learned
    lessons = _get_relevant_lessons(task_id, objective)

    # 3. File context
    files_to_change = data.get("files_to_change", data.get("source_files", []))
    file_context    = _get_file_context(files_to_change)

    # 4. Inject enrichment
    data["enrichment_version"]  = "v2"
    data["enriched_at"]         = _now_iso()
    data["semantic_context"]    = semantic_context
    data["lessons_learned"]     = lessons
    data["file_context"]        = file_context

    # 5. Enhance action plan with context hint
    if semantic_context and data.get("action_plan"):
        data["action_plan"].insert(0, {
            "step":   0,
            "action": f"Review {len(semantic_context)} relevant knowledge base entries injected below",
            "tool":   "read_context",
            "auto":   True,
        })
        for i, item in enumerate(data["action_plan"][1:], 1):
            item["step"] = i

    _log(f"    + {len(semantic_context)} semantic context docs")
    _log(f"    + {len(lessons)} relevant lessons")
    _log(f"    + {len(file_context)} file context entries")

    if not dry_run:
        preflight_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    return True


def enrich_task(task_id: str, dry_run: bool = False) -> int:
    """Enrich all preflight files for a specific task. Returns count enriched."""
    pattern = f"{task_id}-*.json"
    files = list(PREFLIGHT_DIR.glob(pattern))
    if not files:
        _log(f"No preflight files found for {task_id}")
        return 0
    count = 0
    for f in files:
        if enrich_preflight(f, dry_run=dry_run):
            count += 1
    return count


def enrich_all_pending(dry_run: bool = False) -> int:
    """Enrich preflights for all pending/in-progress tasks."""
    if not TASKS_DAG.exists():
        _log("task-dag.json not found")
        return 0

    try:
        dag = json.loads(TASKS_DAG.read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"ERROR reading task-dag: {e}")
        return 0

    active_task_ids = {
        t["id"] for t in dag.get("tasks", [])
        if t.get("status") in {"pending", "in-progress"}
    }

    total = 0
    for pf_file in PREFLIGHT_DIR.glob("*.json"):
        task_id = pf_file.stem.rsplit("-", 1)[0] if "_" in pf_file.stem else pf_file.stem
        # Check if any active task matches this preflight
        for active_id in active_task_ids:
            if pf_file.name.startswith(active_id):
                if enrich_preflight(pf_file, dry_run=dry_run):
                    total += 1
                break

    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC Preflight Enricher v2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-id", metavar="ID",  help="Enrich preflights for a specific task")
    group.add_argument("--all",     action="store_true", help="Enrich all pending task preflights")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    if args.task_id:
        count = enrich_task(args.task_id, dry_run=args.dry_run)
    else:
        count = enrich_all_pending(dry_run=args.dry_run)

    _log(f"Enriched {count} preflight(s)")
    sys.exit(0)
