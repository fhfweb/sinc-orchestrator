import argparse
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from qdrant_client import QdrantClient
except Exception:  # pragma: no cover - optional dependency at runtime
    QdrantClient = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Qdrant quantization canary A/B benchmark and optionally promote alias."
    )
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--qdrant-host", default=os.getenv("QDRANT_HOST", "localhost"))
    parser.add_argument("--qdrant-port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--query-count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline-prefix", default=os.getenv("QDRANT_COLLECTION_PREFIX", ""))
    parser.add_argument("--canary-prefix", default="canary-int8")
    parser.add_argument("--canary-quantization", default="scalar-int8")
    parser.add_argument("--memory-sync-script", default="scripts/memory_sync.py")
    parser.add_argument("--skip-canary-sync", action="store_true")
    parser.add_argument("--sync-baseline", action="store_true")
    parser.add_argument("--reset-canary-collection", action="store_true")
    parser.add_argument("--sync-prune-orphans", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--sync-disable-incremental", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--query-mode", choices=("text", "vector"), default="text")
    parser.add_argument("--ollama-embed-url", default=os.getenv("OLLAMA_EMBED_URL", "http://localhost:11434/v1/embeddings"))
    parser.add_argument("--ollama-embed-model", default=os.getenv("OLLAMA_EMBED_MODEL", "all-minilm:latest"))
    parser.add_argument("--ollama-keep-alive", default=os.getenv("OLLAMA_KEEP_ALIVE", "10m"))
    parser.add_argument("--gate-max-recall-drop-pct", type=float, default=1.5)
    parser.add_argument("--gate-min-p95-improvement-pct", type=float, default=20.0)
    parser.add_argument("--gate-min-memory-reduction-pct", type=float, default=30.0)
    parser.add_argument("--apply-alias", action="store_true")
    parser.add_argument("--rollback-on-fail", action="store_true", default=True)
    parser.add_argument("--alias-name", default="")
    parser.add_argument("--report-path", default="")
    parser.add_argument("--memory-dir", default="memory_graph/nodes")
    parser.add_argument("--relationships-path", default="memory_graph/edges/relationships.md")
    parser.add_argument("--dependency-graph-path", default="")
    parser.add_argument("--world-model-json-path", default="")
    parser.add_argument("--ast-graph-path", default="")
    parser.add_argument("--task-dag-path", default="")
    parser.add_argument("--task-completions-dir", default="")
    parser.add_argument("--qdrant-vector-size", type=int, default=int(os.getenv("QDRANT_VECTOR_SIZE", "768")))
    return parser.parse_args()


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def build_collection_name(prefix: str, project_slug: str) -> str:
    clean_prefix = _clean_text(prefix).strip("-_")
    if clean_prefix and clean_prefix.lower() != project_slug.lower():
        return f"{clean_prefix}-{project_slug}-memory"
    return f"{project_slug}-memory"


def ensure_qdrant_client(host: str, port: int) -> Any:
    if QdrantClient is None:
        raise RuntimeError("qdrant-client is not installed")
    try:
        return QdrantClient(host=host, port=port, check_compatibility=False)
    except TypeError:
        return QdrantClient(host=host, port=port)


def parse_last_json_object(text: str) -> dict[str, Any]:
    text = _clean_text(text)
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for index in range(len(text)):
        if text[index] != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[index:])
        except Exception:
            continue
        if isinstance(obj, dict):
            tail = _clean_text(text[index + end :])
            if not tail:
                return obj
    return {}


def run_memory_sync(
    args: argparse.Namespace,
    collection_prefix: str,
    quantization: str,
    prune_orphans: bool,
    disable_incremental: bool,
) -> dict[str, Any]:
    project_root = Path(args.project_root).resolve()
    script_path_raw = Path(args.memory_sync_script)
    if script_path_raw.is_absolute():
        script_path = script_path_raw
    else:
        candidates = [
            (Path.cwd() / script_path_raw).resolve(),
            (project_root / script_path_raw).resolve(),
            (Path(__file__).resolve().parent / script_path_raw.name).resolve(),
        ]
        script_path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    if not script_path.exists():
        raise RuntimeError(f"memory-sync-script-not-found: {script_path}")

    cmd = [
        sys.executable,
        str(script_path),
        "--project-slug",
        args.project_slug,
        "--project-root",
        str(project_root),
        "--memory-dir",
        args.memory_dir,
        "--relationships-path",
        args.relationships_path,
        "--qdrant-host",
        args.qdrant_host,
        "--qdrant-port",
        str(args.qdrant_port),
        "--qdrant-vector-size",
        str(max(int(args.qdrant_vector_size), 8)),
    ]
    if _clean_text(args.dependency_graph_path):
        cmd.extend(["--dependency-graph-path", args.dependency_graph_path])
    if _clean_text(args.world_model_json_path):
        cmd.extend(["--world-model-json-path", args.world_model_json_path])
    if _clean_text(args.ast_graph_path):
        cmd.extend(["--ast-graph-path", args.ast_graph_path])
    if _clean_text(args.task_dag_path):
        cmd.extend(["--task-dag-path", args.task_dag_path])
    if _clean_text(args.task_completions_dir):
        cmd.extend(["--task-completions-dir", args.task_completions_dir])
    if prune_orphans:
        cmd.append("--qdrant-prune-orphans")
    if disable_incremental:
        cmd.append("--qdrant-disable-incremental-sync")

    env = os.environ.copy()
    env["QDRANT_COLLECTION_PREFIX"] = _clean_text(collection_prefix)
    env["QDRANT_QUANTIZATION"] = _clean_text(quantization)

    proc = subprocess.run(
        cmd,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "memory-sync-failed "
            f"(exit={proc.returncode})\nstdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}"
        )
    parsed = parse_last_json_object(proc.stdout)
    if not parsed:
        raise RuntimeError("memory-sync-output-json-not-found")
    return parsed


def extract_vector(raw: Any) -> list[float]:
    if isinstance(raw, list) and raw:
        return [float(v) for v in raw]
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list) and value:
                return [float(v) for v in value]
    return []


def build_query_text(payload: dict[str, Any], fallback_id: str) -> str:
    summary = _clean_text(payload.get("summary"))
    details = _clean_text(payload.get("details"))
    node_type = _clean_text(payload.get("node_type"))
    relative_path = _clean_text(payload.get("relative_path"))
    node_id = _clean_text(payload.get("node_id")) or fallback_id
    parts = []
    if summary:
        parts.append(summary)
    if details:
        parts.append(details[:500])
    if node_type:
        parts.append(f"type: {node_type}")
    if relative_path:
        parts.append(f"path: {relative_path}")
    if node_id:
        parts.append(f"id: {node_id}")
    return "\n".join(parts) if parts else node_id


@dataclass
class QuerySample:
    point_id: str
    query_text: str
    fallback_vector: list[float]


def collect_query_samples(client: Any, collection_name: str, max_samples: int, seed: int) -> list[QuerySample]:
    wanted = max(int(max_samples), 1)
    scan_target = max(wanted * 4, wanted)
    page_size = 256
    offset = None
    out: list[QuerySample] = []
    seen_ids: set[str] = set()

    while len(out) < scan_target:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            limit=page_size,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break
        for point in points:
            point_id = _clean_text(getattr(point, "id", ""))
            if not point_id or point_id in seen_ids:
                continue
            vector = extract_vector(getattr(point, "vector", None))
            if not vector:
                continue
            payload = getattr(point, "payload", {}) or {}
            query_text = build_query_text(payload if isinstance(payload, dict) else {}, point_id)
            out.append(QuerySample(point_id=point_id, query_text=query_text, fallback_vector=vector))
            seen_ids.add(point_id)
            if len(out) >= scan_target:
                break
        if not next_offset:
            break
        offset = next_offset

    rng = random.Random(seed)
    rng.shuffle(out)
    return out[:wanted]


def _extract_embeddings(payload: Any) -> list[list[float]]:
    if isinstance(payload, list):
        vectors = []
        for item in payload:
            if isinstance(item, list) and item:
                vectors.append([float(v) for v in item])
        return vectors
    if isinstance(payload, dict):
        if isinstance(payload.get("embeddings"), list):
            out = []
            for item in payload.get("embeddings", []):
                if isinstance(item, list) and item:
                    out.append([float(v) for v in item])
            if out:
                return out
        if isinstance(payload.get("data"), list):
            out = []
            for item in payload.get("data", []):
                if isinstance(item, dict) and isinstance(item.get("embedding"), list) and item["embedding"]:
                    out.append([float(v) for v in item["embedding"]])
            if out:
                return out
        if isinstance(payload.get("embedding"), list):
            return [[float(v) for v in payload["embedding"]]]
    return []


def request_embeddings_batch(base_url: str, model: str, texts: list[str], keep_alive: str) -> list[list[float]]:
    if not texts:
        return []
    normalized_texts = [str(t) for t in texts]
    candidates: list[tuple[str, dict[str, Any]]] = []

    clean_url = _clean_text(base_url).rstrip("/")
    if clean_url:
        if clean_url.endswith("/v1/embeddings"):
            candidates.append((clean_url, {"model": model, "input": normalized_texts, "encoding_format": "float"}))
        elif clean_url.endswith("/api/embed"):
            candidates.append((clean_url, {"model": model, "input": normalized_texts, "keep_alive": keep_alive}))
        else:
            candidates.append((clean_url + "/api/embed", {"model": model, "input": normalized_texts, "keep_alive": keep_alive}))
            candidates.append((clean_url + "/v1/embeddings", {"model": model, "input": normalized_texts, "encoding_format": "float"}))

    preferred = [
        ("http://localhost:11434/api/embed", {"model": model, "input": normalized_texts, "keep_alive": keep_alive}),
        ("http://localhost:11434/v1/embeddings", {"model": model, "input": normalized_texts, "encoding_format": "float"}),
    ]
    for endpoint, payload in preferred:
        if not any(endpoint == url for url, _ in candidates):
            candidates.append((endpoint, payload))

    last_error: Exception | None = None
    for endpoint, payload in candidates:
        try:
            response = requests.post(endpoint, json=payload, timeout=120)
            if response.status_code >= 400:
                last_error = RuntimeError(f"{endpoint} -> status {response.status_code}: {response.text[:200]}")
                continue
            vectors = _extract_embeddings(response.json())
            if vectors and len(vectors) >= len(normalized_texts):
                return vectors[: len(normalized_texts)]
            if vectors and len(vectors) == 1 and len(normalized_texts) == 1:
                return vectors
            last_error = RuntimeError(f"{endpoint} -> embeddings-missing-or-invalid")
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"failed-to-embed-batch: {last_error}")


def percentile_ms(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    rank = max(int(math.ceil(q * len(sorted_values))) - 1, 0)
    rank = min(rank, len(sorted_values) - 1)
    return float(sorted_values[rank])


def search_ids(client: Any, collection_name: str, query_vector: list[float], top_k: int) -> tuple[list[str], float]:
    started = time.perf_counter()
    hits: list[Any] = []
    if hasattr(client, "search"):
        kwargs: dict[str, Any] = {
            "collection_name": collection_name,
            "query_vector": query_vector,
            "limit": top_k,
            "with_payload": False,
            "with_vectors": False,
        }
        try:
            hits = list(client.search(**kwargs))
        except TypeError:
            kwargs.pop("with_payload", None)
            kwargs.pop("with_vectors", None)
            hits = list(client.search(**kwargs))
    elif hasattr(client, "query_points"):
        kwargs = {
            "collection_name": collection_name,
            "query": query_vector,
            "limit": top_k,
            "with_payload": False,
            "with_vectors": False,
        }
        response = client.query_points(**kwargs)
        points = getattr(response, "points", None)
        if points is None and isinstance(response, dict):
            points = response.get("points", [])
        hits = list(points or [])
    else:
        raise RuntimeError("qdrant-client-search-method-not-available")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    ids: list[str] = []
    for hit in hits:
        ids.append(_clean_text(getattr(hit, "id", "")))
    return ids, elapsed_ms


def evaluate_collection(
    client: Any,
    collection_name: str,
    queries: list[QuerySample],
    query_vectors: list[list[float]],
    top_k: int,
) -> dict[str, Any]:
    hit_count = 0
    mrr_sum = 0.0
    latencies_ms: list[float] = []
    missing_queries = 0
    evaluated = 0

    for sample, vector in zip(queries, query_vectors):
        if not vector:
            missing_queries += 1
            continue
        ids, elapsed_ms = search_ids(client, collection_name, vector, top_k)
        latencies_ms.append(elapsed_ms)
        evaluated += 1
        if sample.point_id in ids:
            hit_count += 1
            rank = ids.index(sample.point_id) + 1
            mrr_sum += 1.0 / float(rank)

    recall = (hit_count / evaluated) if evaluated else 0.0
    mrr = (mrr_sum / evaluated) if evaluated else 0.0
    p95 = percentile_ms(latencies_ms, 0.95)
    avg_latency = float(statistics.fmean(latencies_ms)) if latencies_ms else 0.0
    return {
        "collection": collection_name,
        "top_k": top_k,
        "queries_total": len(queries),
        "queries_evaluated": evaluated,
        "queries_missing_vector": missing_queries,
        "recall_at_k": round(recall, 6),
        "mrr_at_k": round(mrr, 6),
        "latency_ms_p95": round(p95, 3),
        "latency_ms_avg": round(avg_latency, 3),
        "hit_count": hit_count,
    }


def _extract_vector_size(info: Any) -> int | None:
    if info is None:
        return None

    def walk(value: Any) -> int | None:
        if isinstance(value, dict):
            size = value.get("size")
            if isinstance(size, int) and size > 0:
                return size
            for key in ("vectors", "params", "config", "default", "vector"):
                nested = value.get(key)
                out = walk(nested)
                if out:
                    return out
            for nested in value.values():
                out = walk(nested)
                if out:
                    return out
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                out = walk(item)
                if out:
                    return out
            return None
        if hasattr(value, "model_dump"):
            try:
                return walk(value.model_dump())
            except Exception:
                return None
        if hasattr(value, "dict"):
            try:
                return walk(value.dict())
            except Exception:
                return None
        if hasattr(value, "size"):
            size_attr = getattr(value, "size", None)
            if isinstance(size_attr, int) and size_attr > 0:
                return size_attr
        return None

    return walk(info)


def read_collection_info(client: Any, collection_name: str) -> dict[str, Any]:
    info = client.get_collection(collection_name=collection_name)
    points_count = 0
    indexed_vectors_count = 0
    segments_count = 0
    status = ""
    for field in ("points_count", "indexed_vectors_count", "segments_count", "status"):
        value = getattr(info, field, None)
        if value is None and hasattr(info, "result"):
            value = getattr(info.result, field, None)
        if field == "points_count":
            points_count = int(value or 0)
        elif field == "indexed_vectors_count":
            indexed_vectors_count = int(value or 0)
        elif field == "segments_count":
            segments_count = int(value or 0)
        elif field == "status":
            status = _clean_text(value)

    vector_size = _extract_vector_size(info)
    return {
        "collection": collection_name,
        "status": status or "ok",
        "points_count": points_count,
        "indexed_vectors_count": indexed_vectors_count,
        "segments_count": segments_count,
        "vector_size": int(vector_size or 0),
    }


def estimate_vector_memory_bytes(points_count: int, vector_size: int, quantization_mode: str) -> int:
    dims = max(int(vector_size or 0), 0)
    rows = max(int(points_count or 0), 0)
    mode = _clean_text(quantization_mode).lower()
    bytes_per_dim = 4
    if mode in {"scalar-int8", "int8", "scalar"}:
        bytes_per_dim = 1
    return rows * dims * bytes_per_dim


def normalize_vector(vector: list[float], target_size: int) -> list[float]:
    if target_size <= 0:
        return [float(v) for v in (vector or [])]
    normalized = [float(v) for v in (vector or [])]
    if len(normalized) > target_size:
        return normalized[:target_size]
    if len(normalized) < target_size:
        normalized.extend([0.0] * (target_size - len(normalized)))
    return normalized


def parse_prometheus_labels(label_text: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for part in label_text.split(","):
        segment = part.strip()
        if "=" not in segment:
            continue
        key, raw_value = segment.split("=", 1)
        labels[key.strip()] = raw_value.strip().strip('"')
    return labels


def fetch_collection_memory_metrics(host: str, port: int, collection: str) -> dict[str, float]:
    url = f"http://{host}:{port}/metrics"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code >= 400:
            return {}
        lines = response.text.splitlines()
    except Exception:
        return {}

    selected: dict[str, float] = {}
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "{" not in text or "}" not in text:
            continue
        metric_name, rest = text.split("{", 1)
        labels_part, value_part = rest.split("}", 1)
        labels = parse_prometheus_labels(labels_part)
        collection_label = labels.get("collection") or labels.get("collection_name") or labels.get("collection_id")
        if collection_label != collection:
            continue
        lowered = metric_name.lower()
        if not any(token in lowered for token in ("ram", "memory", "bytes")):
            continue
        try:
            value = float(value_part.strip())
        except Exception:
            continue
        selected[metric_name] = selected.get(metric_name, 0.0) + value
    return selected


def get_alias_map(host: str, port: int) -> dict[str, str]:
    url = f"http://{host}:{port}/aliases"
    try:
        response = requests.get(url, timeout=5)
        if response.status_code >= 400:
            return {}
        payload = response.json() if response.content else {}
    except Exception:
        return {}

    aliases = []
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("aliases"), list):
            aliases = result["aliases"]
        elif isinstance(payload.get("aliases"), list):
            aliases = payload["aliases"]

    out: dict[str, str] = {}
    for entry in aliases:
        if not isinstance(entry, dict):
            continue
        alias_name = _clean_text(entry.get("alias_name"))
        collection_name = _clean_text(entry.get("collection_name"))
        if alias_name and collection_name:
            out[alias_name] = collection_name
    return out


def set_alias_target(host: str, port: int, alias_name: str, collection_name: str, current_target: str) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    if _clean_text(current_target):
        actions.append({"delete_alias": {"alias_name": alias_name}})
    actions.append({"create_alias": {"alias_name": alias_name, "collection_name": collection_name}})

    url = f"http://{host}:{port}/collections/aliases"
    response = requests.post(url, json={"actions": actions}, timeout=10)
    if response.status_code >= 400:
        raise RuntimeError(f"alias-update-failed status={response.status_code} body={response.text[:400]}")
    return {"actions": actions, "status_code": response.status_code}


def delete_collection_if_exists(client: Any, collection_name: str) -> bool:
    existing = {c.name for c in client.get_collections().collections}
    if collection_name not in existing:
        return False
    client.delete_collection(collection_name=collection_name)
    return True


def round_pct(value: float) -> float:
    return round(float(value), 4)


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        raise SystemExit(f"project-root-not-found: {project_root}")

    baseline_collection = build_collection_name(args.baseline_prefix, args.project_slug)
    canary_collection = build_collection_name(args.canary_prefix, args.project_slug)
    alias_name = _clean_text(args.alias_name) or f"{args.project_slug}-memory-active"

    if args.query_count <= 0:
        raise SystemExit("query-count must be > 0")
    if args.top_k <= 0:
        raise SystemExit("top-k must be > 0")

    client = ensure_qdrant_client(args.qdrant_host, args.qdrant_port)

    baseline_sync = {}
    canary_sync = {}
    if args.sync_baseline:
        baseline_sync = run_memory_sync(
            args=args,
            collection_prefix=args.baseline_prefix,
            quantization="none",
            prune_orphans=bool(args.sync_prune_orphans),
            disable_incremental=bool(args.sync_disable_incremental),
        )
    if args.reset_canary_collection and not args.skip_canary_sync:
        delete_collection_if_exists(client, canary_collection)
    if not args.skip_canary_sync:
        canary_sync = run_memory_sync(
            args=args,
            collection_prefix=args.canary_prefix,
            quantization=args.canary_quantization,
            prune_orphans=bool(args.sync_prune_orphans),
            disable_incremental=bool(args.sync_disable_incremental),
        )

    existing = {c.name for c in client.get_collections().collections}
    if baseline_collection not in existing:
        raise SystemExit(f"baseline-collection-not-found: {baseline_collection}")
    if canary_collection not in existing:
        raise SystemExit(f"canary-collection-not-found: {canary_collection}")

    baseline_info = read_collection_info(client, baseline_collection)
    canary_info = read_collection_info(client, canary_collection)
    baseline_vector_size = max(int(baseline_info.get("vector_size", 0) or 0), 0)
    canary_vector_size = max(int(canary_info.get("vector_size", 0) or 0), 0)

    queries = collect_query_samples(client, baseline_collection, args.query_count, args.seed)
    if not queries:
        raise SystemExit("no-query-samples-collected")

    query_vectors_raw: list[list[float]] = []
    query_mode_runtime = args.query_mode
    if args.query_mode == "text":
        texts = [q.query_text for q in queries]
        try:
            embeddings = request_embeddings_batch(
                base_url=args.ollama_embed_url,
                model=args.ollama_embed_model,
                texts=texts,
                keep_alive=args.ollama_keep_alive,
            )
            if len(embeddings) >= len(queries):
                query_vectors_raw = embeddings[: len(queries)]
            else:
                query_vectors_raw = [q.fallback_vector for q in queries]
                query_mode_runtime = "vector-fallback"
        except Exception:
            query_vectors_raw = [q.fallback_vector for q in queries]
            query_mode_runtime = "vector-fallback"
    else:
        query_vectors_raw = [q.fallback_vector for q in queries]

    baseline_query_vectors = [normalize_vector(vector, baseline_vector_size) for vector in query_vectors_raw]
    canary_query_vectors = [normalize_vector(vector, canary_vector_size) for vector in query_vectors_raw]

    baseline_eval = evaluate_collection(client, baseline_collection, queries, baseline_query_vectors, args.top_k)
    canary_eval = evaluate_collection(client, canary_collection, queries, canary_query_vectors, args.top_k)

    baseline_estimated_bytes = estimate_vector_memory_bytes(
        baseline_info.get("points_count", 0),
        baseline_info.get("vector_size", 0),
        "none",
    )
    canary_estimated_bytes = estimate_vector_memory_bytes(
        canary_info.get("points_count", 0),
        canary_info.get("vector_size", 0),
        args.canary_quantization,
    )

    baseline_metrics = fetch_collection_memory_metrics(args.qdrant_host, args.qdrant_port, baseline_collection)
    canary_metrics = fetch_collection_memory_metrics(args.qdrant_host, args.qdrant_port, canary_collection)

    def select_observed_total(metrics: dict[str, float]) -> float:
        if not metrics:
            return 0.0
        return float(sum(metrics.values()))

    observed_baseline_mem = select_observed_total(baseline_metrics)
    observed_canary_mem = select_observed_total(canary_metrics)

    baseline_points = max(int(baseline_info.get("points_count", 0) or 0), 0)
    canary_points = max(int(canary_info.get("points_count", 0) or 0), 0)
    points_max = max(baseline_points, canary_points, 1)
    points_diff_pct = abs(baseline_points - canary_points) / points_max * 100.0
    points_mismatch = points_diff_pct > 5.0

    memory_source = "estimated"
    baseline_memory_for_gate = float(baseline_estimated_bytes)
    canary_memory_for_gate = float(canary_estimated_bytes)
    if observed_baseline_mem > 0.0 and observed_canary_mem > 0.0 and not points_mismatch:
        memory_source = "observed_prometheus"
        baseline_memory_for_gate = observed_baseline_mem
        canary_memory_for_gate = observed_canary_mem
    elif points_mismatch:
        baseline_bytes_per_point = (baseline_estimated_bytes / baseline_points) if baseline_points > 0 else 0.0
        canary_bytes_per_point = (canary_estimated_bytes / canary_points) if canary_points > 0 else 0.0
        baseline_memory_for_gate = baseline_bytes_per_point
        canary_memory_for_gate = canary_bytes_per_point
        memory_source = "estimated_per_point_due_dataset_mismatch"

    baseline_recall = float(baseline_eval.get("recall_at_k", 0.0))
    canary_recall = float(canary_eval.get("recall_at_k", 0.0))
    recall_drop_pct_points = max(0.0, (baseline_recall - canary_recall) * 100.0)
    vector_size_mismatch = baseline_vector_size > 0 and canary_vector_size > 0 and baseline_vector_size != canary_vector_size

    baseline_p95 = float(baseline_eval.get("latency_ms_p95", 0.0))
    canary_p95 = float(canary_eval.get("latency_ms_p95", 0.0))
    if baseline_p95 > 0.0:
        p95_improvement_pct = ((baseline_p95 - canary_p95) / baseline_p95) * 100.0
    else:
        p95_improvement_pct = 0.0

    if baseline_memory_for_gate > 0.0:
        memory_reduction_pct = ((baseline_memory_for_gate - canary_memory_for_gate) / baseline_memory_for_gate) * 100.0
    else:
        memory_reduction_pct = 0.0

    gate = {
        "max_recall_drop_pct": float(args.gate_max_recall_drop_pct),
        "min_p95_improvement_pct": float(args.gate_min_p95_improvement_pct),
        "min_memory_reduction_pct": float(args.gate_min_memory_reduction_pct),
        "dataset_points_diff_pct": round_pct(points_diff_pct),
        "dataset_points_mismatch": bool(points_mismatch),
        "baseline_vector_size": baseline_vector_size,
        "canary_vector_size": canary_vector_size,
        "vector_size_mismatch": bool(vector_size_mismatch),
        "actual_recall_drop_pct": round_pct(recall_drop_pct_points),
        "actual_p95_improvement_pct": round_pct(p95_improvement_pct),
        "actual_memory_reduction_pct": round_pct(memory_reduction_pct),
        "recall_pass": recall_drop_pct_points <= float(args.gate_max_recall_drop_pct),
        "p95_pass": p95_improvement_pct >= float(args.gate_min_p95_improvement_pct),
        "memory_pass": memory_reduction_pct >= float(args.gate_min_memory_reduction_pct),
        "compatibility_pass": not bool(vector_size_mismatch),
    }
    gate["pass"] = bool(gate["recall_pass"] and gate["p95_pass"] and gate["memory_pass"] and gate["compatibility_pass"])

    alias_before = get_alias_map(args.qdrant_host, args.qdrant_port)
    current_alias_target = alias_before.get(alias_name, "")
    alias_action = {
        "apply_alias": bool(args.apply_alias),
        "alias_name": alias_name,
        "before_target": current_alias_target,
        "after_target": current_alias_target,
        "operation": "noop",
        "result": {},
    }

    desired_target = current_alias_target
    decision = "keep-baseline"
    if gate["pass"]:
        decision = "promote-canary"
        desired_target = canary_collection
    elif bool(args.rollback_on_fail):
        desired_target = baseline_collection

    if args.apply_alias and desired_target and desired_target != current_alias_target:
        result = set_alias_target(
            host=args.qdrant_host,
            port=args.qdrant_port,
            alias_name=alias_name,
            collection_name=desired_target,
            current_target=current_alias_target,
        )
        alias_action["after_target"] = desired_target
        alias_action["operation"] = "set-alias"
        alias_action["result"] = result

    report = {
        "generated_at": iso_now(),
        "project_slug": args.project_slug,
        "project_root": str(project_root),
        "query_mode_requested": args.query_mode,
        "query_mode_runtime": query_mode_runtime,
        "query_count": len(queries),
        "top_k": args.top_k,
        "collections": {
            "baseline": baseline_collection,
            "canary": canary_collection,
        },
        "sync": {
            "baseline": baseline_sync,
            "canary": canary_sync,
            "sync_baseline": bool(args.sync_baseline),
            "skip_canary_sync": bool(args.skip_canary_sync),
            "reset_canary_collection": bool(args.reset_canary_collection),
            "sync_prune_orphans": bool(args.sync_prune_orphans),
            "sync_disable_incremental": bool(args.sync_disable_incremental),
            "canary_quantization": args.canary_quantization,
            "canary_prefix": args.canary_prefix,
        },
        "baseline_metrics": baseline_eval,
        "canary_metrics": canary_eval,
        "collection_info": {
            "baseline": baseline_info,
            "canary": canary_info,
        },
        "memory": {
            "source_for_gate": memory_source,
            "points": {
                "baseline": baseline_points,
                "canary": canary_points,
                "diff_pct": round_pct(points_diff_pct),
                "mismatch": bool(points_mismatch),
            },
            "estimated": {
                "baseline_vector_bytes": baseline_estimated_bytes,
                "canary_vector_bytes": canary_estimated_bytes,
            },
            "observed_prometheus": {
                "baseline": baseline_metrics,
                "canary": canary_metrics,
            },
        },
        "gate": gate,
        "decision": decision,
        "alias": alias_action,
    }

    report_path = _clean_text(args.report_path)
    if not report_path:
        report_dir = project_root / "ai-orchestrator" / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        report_path = str((report_dir / f"qdrant-canary-ab-{stamp}.json").resolve())
    report_file = Path(report_path)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = {
        "ok": True,
        "report_path": str(report_file),
        "gate_pass": bool(gate["pass"]),
        "decision": decision,
        "recall_drop_pct": gate["actual_recall_drop_pct"],
        "p95_improvement_pct": gate["actual_p95_improvement_pct"],
        "memory_reduction_pct": gate["actual_memory_reduction_pct"],
        "alias_target_after": alias_action["after_target"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
