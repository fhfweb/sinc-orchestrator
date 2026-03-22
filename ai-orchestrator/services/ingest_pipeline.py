from services.streaming.core.config import env_get
"""
Ingest Pipeline Worker
======================
Watches for DISPATCHES/ingest-*.json files, runs the full ingest pipeline:
  1. AST analysis → Neo4j knowledge graph
  2. Chunking → Qdrant vector store
  3. Updates ingest_pipelines table in PostgreSQL
  4. Broadcasts SSE events on progress/completion

Dispatch file format (written by POST /ingest or PowerShell):
  {
    "pipeline_id": "ingest-<uuid>",
    "project_id":  "sinc",
    "tenant_id":   "local",
    "project_path": "/path/to/project",
    "deep": true
  }

Usage:
    # As a standalone worker (watches DISPATCHES/ directory)
    python ingest_pipeline.py --watch /path/to/dispatches --interval 10

    # Or import and call directly
    from services.ingest_pipeline import IngestPipeline
    pipeline = IngestPipeline()
    pipeline.run("ingest-abc123", project_path="/app", project_id="sinc", tenant_id="local")
"""

import logging
import os
import json
import re as _re
import shutil
import subprocess
import tempfile
import time
import uuid
import hashlib
import threading
import asyncio
from typing import Any, Dict, List, Optional, Callable, Tuple
from services.xref_resolver import XRefResolver
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator")

_ID_RE = _re.compile(r'^[a-zA-Z0-9_\-.]{1,128}$')


def _validate_id(value: str, field: str) -> str:
    """Reject values that could be used for path traversal or injection."""
    if not _ID_RE.match(value):
        raise ValueError(f"Invalid {field}: {value!r}")
    return value

# ── OpenTelemetry ─────────────────────────────────────────────────────────────
try:
    from services.otel_setup import configure_otel, span as _span
    configure_otel("ingest-worker")
except ImportError:
    from contextlib import nullcontext
    def _span(_name: str, **_kw):  # type: ignore[misc]
        return nullcontext()

import httpx as _httpx
# tenacity and other retries are handled by the resilient client or explicitly in the logic

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

from services.streaming.core.config import (
    env_get, QDRANT_HOST, QDRANT_PORT, OLLAMA_HOST,
    CHUNK_SIZE, CHUNK_OVERLAP, DISPATCHES_DIR,
    NEO4J_URI, NEO4J_USER, NEO4J_PASS
)
from services.http_client import create_sync_resilient_client

# ── DATABASE HELPERS ──────────────────────────────────────────────────────

def _db_conn():
    """Return a database connection using the unified db context manager."""
    from services.streaming.core.db import db
    return db(bypass_rls=True)


def _update_pipeline(pipeline_id: str, status: str, progress: int = 0,
                     error: str = "", stats: Optional[dict] = None):
    """Update ingest_pipelines row in PostgreSQL."""
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ingest_pipelines
                    SET status = %s, progress = %s, error = %s,
                        stats = %s, updated_at = NOW(),
                        finished_at = CASE WHEN %s IN ('done','failed') THEN NOW() ELSE finished_at END
                    WHERE id = %s
                """, (status, progress, error,
                      json.dumps(stats or {}), status, pipeline_id))
                conn.commit()
    except Exception as exc:
        print(f"[ingest] DB update error: {exc}")


def _insert_pipeline(pipeline_id: str, project_id: str, tenant_id: str,
                     project_path: str, deep: bool):
    """Insert or replace pipeline record."""
    try:
        with _db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ingest_pipelines
                        (id, project_id, tenant_id, project_path, deep, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'running', NOW(), NOW())
                    ON CONFLICT (id) DO UPDATE
                        SET status = 'running', updated_at = NOW()
                """, (pipeline_id, project_id, tenant_id, project_path, deep))
                conn.commit()
    except Exception as exc:
        print(f"[ingest] DB insert error: {exc}")


# ──────────────────────────────────────────────
# CHUNKER
# ──────────────────────────────────────────────

def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping word-boundary chunks."""
    words  = text.split()
    chunks = []
    start  = 0
    while start < len(words):
        end = start + size
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.append(chunk)
        start += size - overlap
    return chunks


# ──────────────────────────────────────────────
# EMBEDDING
# ──────────────────────────────────────────────

def _embed_one(text: str) -> list[float]:
    """Embed a single text via Ollama using the resilient client."""
    payload = {"model": EMBED_MODEL, "prompt": text}
    with create_sync_resilient_client(service_name="ingest-ollama", timeout=30) as client:
        resp = client.post(f"{OLLAMA_HOST}/api/embeddings", json=payload)
        resp.raise_for_status()
        return resp.json().get("embedding", [])


def _embed(texts: list[str]) -> list[list[float]]:
    """Get embeddings with retry logic (if provided by _embed_one)."""
    embeddings = []
    for text in texts:
        try:
            embeddings.append(_embed_one(text))
        except Exception as exc:
            print(f"[ingest] embed error: {exc}", flush=True)
            embeddings.append([])
    return embeddings


def _embed_dim() -> int:
    """Probe embedding dimension (nomic-embed-text = 768)."""
    sample = _embed(["probe"])
    if sample and sample[0]:
        return len(sample[0])
    return 768


# ──────────────────────────────────────────────
# QDRANT UPSERT
# ──────────────────────────────────────────────

def _qdrant_collection_name(project_id: str, tenant_id: str) -> str:
    return f"{tenant_id}_{project_id}_code"


def _ensure_qdrant_collection(collection: str, dim: int):
    """Create Qdrant collection if it doesn't exist using the resilient client."""
    url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{collection}"
    try:
        with create_sync_resilient_client(service_name="ingest-qdrant", timeout=10) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                return
            if resp.status_code == 404:
                payload = {"vectors": {"size": dim, "distance": "Cosine"}}
                client.put(url, json=payload).raise_for_status()
                print(f"[ingest] created Qdrant collection: {collection}", flush=True)
    except Exception as exc:
        print(f"[ingest] Qdrant collection management error: {exc}", flush=True)


def _qdrant_upsert(collection: str, points: list[dict]):
    """Batch upsert points into Qdrant using the resilient client."""
    if not points:
        return
    url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{collection}/points"
    try:
        with create_sync_resilient_client(service_name="ingest-qdrant", timeout=30) as client:
            client.put(url, json={"points": points}).raise_for_status()
    except Exception as exc:
        log.error("ingest_qdrant_upsert_error error=%s", exc)


# ──────────────────────────────────────────────
# FILE WALKER (mirrors ast_analyzer._walk_source_files)
# ──────────────────────────────────────────────

_LANG_EXTENSIONS = {".php", ".py", ".js", ".ts", ".go", ".md", ".txt", ".yaml", ".yml", ".json"}
_SKIP_DIRS = {"vendor", "node_modules", ".git", "storage", "bootstrap/cache",
              "__pycache__", ".venv", "venv", "dist", "build", ".next"}


def _walk_files(root: str):
    """Yield (abs_path, rel_path) for each indexable source file."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext in _LANG_EXTENSIONS:
                abs_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(abs_path, root).replace("\\", "/")
                yield abs_path, rel_path


# ──────────────────────────────────────────────
# PIPELINE
# ──────────────────────────────────────────────

class IngestPipeline:
    """
    Runs the full ingest pipeline for a project:
      1. AST analysis → Neo4j
      2. Chunking + embedding → Qdrant
    """

    def __init__(self, on_event: Optional[Callable[[str, dict], None]] = None):
        """
        on_event(event_type, payload) — called for SSE broadcast.
        event_type: 'ingest_started' | 'ingest_progress' | 'ingest_done' | 'ingest_failed'
        """
        self.on_event = on_event or (lambda t, p: None)

    def _emit(self, event_type: str, payload: dict):
        try:
            self.on_event(event_type, payload)
        except Exception:
            pass

    # ── Git clone support ────────────────────────────────────────────────────

    @staticmethod
    def _is_git_url(path: str) -> bool:
        return path.startswith(("http://", "https://", "git@", "git://", "ssh://"))

    @staticmethod
    def _clone(repo_url: str, branch: str = "") -> str:
        """
        Clone repo_url into a temp directory.
        Returns the temp directory path (caller must clean up).
        Raises RuntimeError on failure.
        """
        base = env_get("GIT_CLONE_BASE", default=tempfile.gettempdir())
        clone_dir = tempfile.mkdtemp(prefix="ingest-", dir=base)
        cmd = ["git", "clone", "--depth", "1", "--single-branch"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [repo_url, clone_dir]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                shutil.rmtree(clone_dir, ignore_errors=True)
                raise RuntimeError(f"git clone failed: {result.stderr[:500]}")
            return clone_dir
        except FileNotFoundError:
            shutil.rmtree(clone_dir, ignore_errors=True)
            raise RuntimeError("git not found — install git in the container")

    # ─────────────────────────────────────────────────────────────────────────

    def run(self, pipeline_id: str, project_path: str,
            project_id: str, tenant_id: str, deep: bool = True,
            repo_url: str = "", branch: str = "") -> dict:
        """
        Execute the pipeline. Returns stats dict.
        Updates ingest_pipelines table and emits SSE events.

        If project_path is a git URL (or repo_url is provided),
        the repo is cloned to a temp directory and cleaned up after ingest.
        """
        _validate_id(project_id, "project_id")
        _validate_id(tenant_id, "tenant_id")

        # Resolve source: git URL → clone to temp dir
        clone_dir: Optional[str] = None
        effective_url = repo_url or (project_path if self._is_git_url(project_path) else "")
        if effective_url:
            try:
                clone_dir   = self._clone(effective_url, branch)
                project_path = clone_dir
            except RuntimeError as e:
                _insert_pipeline(pipeline_id, project_id, tenant_id, effective_url, deep)
                _update_pipeline(pipeline_id, "failed", error=str(e))
                self._emit("ingest_failed", {"pipeline_id": pipeline_id, "error": str(e)})
                return {"ast": {}, "chunks": 0, "vectors": 0, "files": 0, "errors": 1}

        _insert_pipeline(pipeline_id, project_id, tenant_id, project_path, deep)
        self._emit("ingest_started", {
            "pipeline_id": pipeline_id, "project_id": project_id,
            "tenant_id": tenant_id, "project_path": project_path,
            "repo_url": effective_url,
        })
        stats = {"ast": {}, "chunks": 0, "vectors": 0, "files": 0, "errors": 0}

        try:
            # ── Step 1: AST → Neo4j ──────────────────
            _update_pipeline(pipeline_id, "running_ast", 10)
            self._emit("ingest_progress", {
                "pipeline_id": pipeline_id, "step": "ast", "progress": 10
            })
            with _span("ingest.ast", project_id=project_id):
                ast_stats = self._run_ast(project_path, project_id, tenant_id)
            stats["ast"] = ast_stats
            stats["files"] = ast_stats.get("files", 0)
            _update_pipeline(pipeline_id, "running_vectors", 40, stats=stats)
            self._emit("ingest_progress", {
                "pipeline_id": pipeline_id, "step": "vectors", "progress": 40,
                "ast_files": ast_stats.get("files", 0),
            })

            # ── Step 1.5: XRef Global Resolution ─────
            _update_pipeline(pipeline_id, "running_xref", 30)
            self._emit("ingest_progress", {"pipeline_id": pipeline_id, "step": "xref", "progress": 30})
            with _span("ingest.xref", project_id=project_id):
                try:
                    xref = XRefResolver(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASS)
                    xref_stats = xref.run_all(tenant_id)
                    xref.close()
                    stats["xref"] = xref_stats
                except Exception as xref_err:
                    print(f"[ingest] xref resolution failed: {xref_err}")

            # ── Step 2: Chunk + Embed → Qdrant ───────
            if deep:
                with _span("ingest.vectors", project_id=project_id):
                    # Pass symbols from AST to vector phase for Docstring indexing
                    symbols = stats.get("ast", {}).get("symbols")
                    vec_stats = self._run_vectors(project_path, project_id, tenant_id, pipeline_id, symbols=symbols)
                stats["chunks"]  = vec_stats["chunks"]
                stats["vectors"] = vec_stats["vectors"]
                stats["errors"] += vec_stats["errors"]

            _update_pipeline(pipeline_id, "done", 100, stats=stats)
            self._emit("ingest_done", {
                "pipeline_id": pipeline_id,
                "project_id":  project_id,
                "stats":       stats,
            })

        except Exception as exc:
            stats["errors"] += 1
            error_msg = str(exc)
            _update_pipeline(pipeline_id, "failed", error=error_msg, stats=stats)
            self._emit("ingest_failed", {
                "pipeline_id": pipeline_id,
                "error": error_msg,
            })
            print(f"[ingest] pipeline {pipeline_id} failed: {exc}")
        finally:
            # Clean up git clone temp dir
            if clone_dir:
                shutil.rmtree(clone_dir, ignore_errors=True)

        return stats

    def _run_ast(self, project_path: str, project_id: str, tenant_id: str) -> dict:
        """Run AST analyzer and return stats."""
        try:
            from services.ast_analyzer import ASTAnalyzer
            with ASTAnalyzer(neo4j_uri=NEO4J_URI, neo4j_auth=(NEO4J_USER, NEO4J_PASS)) as analyzer:
                return analyzer.analyze_project(
                    project_path, project_id=project_id, tenant_id=tenant_id
                )
        except ImportError:
            print("[ingest] ast_analyzer not available — skipping Neo4j step")
            return {"files": 0, "nodes_created": 0, "edges_created": 0, "errors": 0}
        except Exception as exc:
            print(f"[ingest] AST error: {exc}")
            return {"files": 0, "nodes_created": 0, "edges_created": 0, "errors": 1}

    def _run_vectors(self, project_path: str, project_id: str,
                     tenant_id: str, _pipeline_id: str = "",
                     symbols: Any = None) -> dict:
        """Chunk files, embed, upsert to Qdrant. Also indexes docstrings if provided."""
        collection = _qdrant_collection_name(project_id, tenant_id)
        stats = {"chunks": 0, "vectors": 0, "errors": 0}

        # Probe embedding dimension
        try:
            dim = _embed_dim()
        except Exception:
            dim = 768
        _ensure_qdrant_collection(collection, dim)

        batch_texts    = []
        batch_payloads = []
        batch_ids      = []
        BATCH_SIZE     = 32

        def flush_batch():
            if not batch_texts:
                return
            embeddings = _embed(batch_texts)
            points = []
            for idx, (emb, payload, point_id) in enumerate(zip(embeddings, batch_payloads, batch_ids)):
                if emb:
                    points.append({"id": point_id, "vector": emb, "payload": payload})
                    stats["vectors"] += 1
            _qdrant_upsert(collection, points)
            batch_texts.clear()
            batch_payloads.clear()
            batch_ids.clear()

        for abs_path, rel_path in _walk_files(project_path):
            try:
                content = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                stats["errors"] += 1
                continue

            chunks = _chunk_text(content)
            for i, chunk in enumerate(chunks):
                # Deterministic UUID from (project_id, tenant_id, rel_path, chunk_index)
                seed  = f"{tenant_id}/{project_id}/{rel_path}/{i}"
                pt_id = str(uuid.UUID(hashlib.md5(seed.encode()).hexdigest()))
                batch_ids.append(pt_id)
                batch_texts.append(chunk)
                batch_payloads.append({
                    "project_id": project_id,
                    "tenant_id":  tenant_id,
                    "file":       rel_path,
                    "chunk":      i,
                    "text":       chunk[:500],   # store first 500 chars as preview
                })
                stats["chunks"] += 1

                if len(batch_texts) >= BATCH_SIZE:
                    flush_batch()

        flush_batch()
        
        # --- PHASE 7: Index Docstrings (Cognitive Intent) ---
        if symbols:
            print(f"[ingest] indexing docstrings for deep memory...", flush=True)
            for sym_id, sym in getattr(symbols, "symbols", {}).items():
                if sym.docstring and len(sym.docstring) > 10:
                    pt_id = str(uuid.UUID(hashlib.md5(f"doc/{sym_id}".encode()).hexdigest()))
                    batch_ids.append(pt_id)
                    batch_texts.append(f"Documentation for {sym.name} in {sym.file_path}:\n{sym.docstring}")
                    batch_payloads.append({
                        "project_id": project_id,
                        "tenant_id":  tenant_id,
                        "file":       sym.file_path,
                        "type":       "docstring",
                        "symbol":     sym.name,
                        "text":       sym.docstring,
                    })
                    if len(batch_texts) >= BATCH_SIZE:
                        flush_batch()
            flush_batch()

        return stats


# ──────────────────────────────────────────────
# REDIS STREAM CONSUMER (REPLACES DISPATCHWATCHER)
# ──────────────────────────────────────────────

class IngestStreamConsumer:
    """
    Elite Consumer for Ingest Tasks.
    Listens to 'sinc:stream:ingest' via Redis Streams.
    """
    def __init__(self, pipeline: IngestPipeline):
        self.pipeline = pipeline

    async def start(self):
        from services.event_bus import get_event_bus
        bus = await get_event_bus()
        
        log_ingest = logging.getLogger("orch.ingest_worker")
        log_ingest.info("IngestStreamConsumer starting (Redis Streams)...")
        
        # This will block and handle auto-claims/acks internally
        await bus.consume(
            stream_name="sinc:stream:ingest",
            group_name="ingest_workers",
            consumer_name=f"worker-{uuid.uuid4().hex[:6]}",
            callback=self._process_event
        )

    async def _process_event(self, data: dict):
        pipeline_id  = data.get("pipeline_id", f"ingest-{uuid.uuid4().hex[:8]}")
        project_path = data.get("project_path", "")
        project_id   = data.get("project_id", "default")
        tenant_id    = data.get("tenant_id",  "local")
        deep         = bool(data.get("deep", True))
        repo_url     = data.get("repo_url", "")
        branch       = data.get("branch",   "")

        log.info("ingest_stream_processing pipeline_id=%s project=%s tenant=%s", 
                 pipeline_id, project_id, tenant_id)
        
        # Run the pipeline (blocking for now as it's a dedicated worker)
        # We hook the broadcast into the pipeline's on_event
        self.pipeline.on_event = lambda et, payload: asyncio.run_coroutine_threadsafe(
            broadcast(et, payload, tenant_id=tenant_id),
            asyncio.get_event_loop()
        ).result() if et and payload else None

        await asyncio.to_thread(
            self.pipeline.run,
            pipeline_id, project_path=project_path,
            project_id=project_id, tenant_id=tenant_id, deep=deep,
            repo_url=repo_url, branch=branch
        )


# ──────────────────────────────────────────────
# STANDALONE ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys, argparse

    def _log_event(event_type: str, payload: dict):
        print(f"[{event_type}] {json.dumps(payload)}")

    pipeline = IngestPipeline(on_event=_log_event)

    async def run_worker():
        consumer = IngestStreamConsumer(pipeline)
        await consumer.start()

    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        print("[ingest-worker] stopped")
