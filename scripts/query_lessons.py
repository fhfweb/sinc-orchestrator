"""
query_lessons.py - semantic/keyword search for reusable fixes and patterns.

Priority order:
1) Project memory collection (<project-slug>-memory)
2) Global memory collection (orchestrator-global-memory)
3) Legacy docs index (orchestrator_lessons)
4) Keyword fallback
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        Distance,
        FieldCondition,
        Filter,
        MatchValue,
        PointStruct,
        VectorParams,
    )

    QDRANT_AVAILABLE = True
except ImportError:
    QDRANT_AVAILABLE = False
    QdrantClient = None
    Distance = None
    PointStruct = None
    VectorParams = None
    Filter = None
    FieldCondition = None
    MatchValue = None

try:
    from sentence_transformers import SentenceTransformer

    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    SentenceTransformer = None


COLLECTION_NAME = "orchestrator_lessons"
GLOBAL_COLLECTION_DEFAULT = "orchestrator-global-memory"
EMBEDDING_MODEL = os.getenv("QUERY_LESSONS_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
VECTOR_DIM = 384
OLLAMA_EMBED_URL = os.getenv("OLLAMA_EMBED_URL", "http://localhost:11434/v1/embeddings")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "all-minilm:latest")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "10m")
USE_SENTENCE_TRANSFORMERS = os.getenv("QUERY_LESSONS_USE_SENTENCE_TRANSFORMERS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def get_sentence_transformer_model() -> Any | None:
    if not EMBEDDINGS_AVAILABLE or not USE_SENTENCE_TRANSFORMERS:
        return None
    try:
        return SentenceTransformer(EMBEDDING_MODEL, local_files_only=True)
    except TypeError:
        try:
            return SentenceTransformer(EMBEDDING_MODEL)
        except Exception:
            return None
    except Exception:
        return None


def slugify(value: str) -> str:
    lowered = (value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not cleaned:
        return "project"
    return cleaned[:63]


def derive_project_slug(project_path: Path) -> str:
    state_path = project_path / "ai-orchestrator" / "project-state.json"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in ("project_slug", "slug"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        return slugify(value)
                if isinstance(data.get("project"), dict):
                    nested = data["project"].get("slug")
                    if isinstance(nested, str) and nested.strip():
                        return slugify(nested)
        except Exception:
            pass
    return slugify(project_path.name)


def resolve_project_collection_name(project_slug: str) -> str:
    prefix_value = (os.getenv("QDRANT_COLLECTION_PREFIX", "") or "").strip().strip("-_")
    if prefix_value and prefix_value.lower() != project_slug.lower():
        return f"{prefix_value}-{project_slug}-memory"
    return f"{project_slug}-memory"


def load_documents(project_path: Path) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    repo_root = Path(__file__).resolve().parents[1]
    search_roots = [
        project_path / "ai-orchestrator" / "knowledge_base" / "lessons_learned",
        project_path / "ai-orchestrator" / "patterns",
        project_path.parent / "memory_graph" / "patterns",
        repo_root / "memory_graph" / "patterns",
    ]

    for root in search_roots:
        if not root.exists():
            continue
        for md_file in sorted(root.glob("*.md")):
            try:
                text = md_file.read_text(encoding="utf-8", errors="ignore")
                if len(text.strip()) < 20:
                    continue
                title_match = re.search(r"^#+\s+(.+)", text, re.MULTILINE)
                title = title_match.group(1).strip() if title_match else md_file.stem
                if md_file.is_relative_to(project_path):
                    relative_path = str(md_file.relative_to(project_path))
                else:
                    relative_path = str(md_file)
                docs.append(
                    {
                        "id": md_file.stem,
                        "title": title,
                        "path": str(md_file),
                        "relative_path": relative_path,
                        "text": text,
                        "source": root.name,
                    }
                )
            except Exception:
                continue
    return docs


def keyword_search(query: str, docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    query_tokens = set(re.findall(r"\w+", query.lower()))
    scored: list[tuple[float, dict[str, Any]]] = []
    for doc in docs:
        text_lower = doc["text"].lower()
        title_lower = doc["title"].lower()
        doc_tokens = set(re.findall(r"\w+", text_lower))
        overlap = len(query_tokens & doc_tokens)
        title_overlap = len(query_tokens & set(re.findall(r"\w+", title_lower)))
        score = overlap + title_overlap * 3
        scored.append((float(score), doc))

    scored.sort(key=lambda item: item[0], reverse=True)
    results: list[dict[str, Any]] = []
    for score, doc in scored[:top_k]:
        if score <= 0:
            break
        snippet = doc["text"][:400].replace("\n", " ")
        results.append(
            {
                "score": round(score / max(1, len(query_tokens)), 3),
                "method": "keyword",
                "id": doc["id"],
                "title": doc["title"],
                "path": doc["relative_path"],
                "snippet": snippet,
                "source": "docs-keyword",
            }
        )
    return results


def normalize_embedding(values: list[float], target_size: int) -> list[float]:
    target = max(int(target_size), 8)
    if not values:
        return [0.0] * target
    padded = list(values[:target])
    if len(padded) < target:
        padded.extend([0.0] * (target - len(padded)))
    norm = math.sqrt(sum(v * v for v in padded))
    if norm <= 0.0:
        return [0.0] * target
    return [v / norm for v in padded]


def build_local_embedding(text: str, vector_size: int) -> list[float]:
    target_size = max(int(vector_size), 8)
    signal = [0.0] * target_size
    tokens = re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
    if not tokens:
        digest = hashlib.sha256((text or "empty").encode("utf-8")).digest()
        signal = [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(target_size)]
        return normalize_embedding(signal, target_size)

    for idx, token in enumerate(tokens):
        digest = hashlib.sha256(f"{idx}:{token}".encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], "big", signed=False) % target_size
        sign = 1.0 if (digest[2] % 2 == 0) else -1.0
        weight = 0.75 + (digest[3] / 255.0)
        signal[bucket] += sign * weight
    return normalize_embedding(signal, target_size)


def _extract_embedding(payload: dict[str, Any] | None) -> list[float] | None:
    if not isinstance(payload, dict):
        return None
    direct = payload.get("embedding")
    if isinstance(direct, list) and direct:
        return [float(v) for v in direct]
    batched = payload.get("embeddings")
    if isinstance(batched, list) and batched:
        first = batched[0]
        if isinstance(first, list) and first:
            return [float(v) for v in first]
    openai_data = payload.get("data")
    if isinstance(openai_data, list) and openai_data:
        first_item = openai_data[0]
        if isinstance(first_item, dict):
            vector = first_item.get("embedding")
            if isinstance(vector, list) and vector:
                return [float(v) for v in vector]
    return None


def request_ollama_embedding(text: str) -> list[float]:
    normalized = (OLLAMA_EMBED_URL or "").strip().rstrip("/")
    if not normalized:
        raise RuntimeError("OLLAMA_EMBED_URL is empty")

    base_url = normalized
    for marker in ("/api/", "/v1/"):
        marker_index = normalized.lower().find(marker)
        if marker_index >= 0:
            base_url = normalized[:marker_index]
            break

    endpoints = [normalized]
    if normalized.endswith("/api/embeddings"):
        endpoints.append(normalized[: -len("/api/embeddings")] + "/api/embed")
        endpoints.append(normalized[: -len("/api/embeddings")] + "/v1/embeddings")
    elif normalized.endswith("/api/embed"):
        endpoints.append(normalized[: -len("/api/embed")] + "/api/embeddings")
        endpoints.append(normalized[: -len("/api/embed")] + "/v1/embeddings")
    elif normalized.endswith("/v1/embeddings"):
        endpoints.append(normalized[: -len("/v1/embeddings")] + "/api/embeddings")
        endpoints.append(normalized[: -len("/v1/embeddings")] + "/api/embed")
    else:
        endpoints.append(f"{base_url}/api/embeddings")
        endpoints.append(f"{base_url}/api/embed")
        endpoints.append(f"{base_url}/v1/embeddings")

    seen: set[str] = set()
    ordered: list[str] = []
    for endpoint in endpoints:
        if endpoint and endpoint not in seen:
            ordered.append(endpoint)
            seen.add(endpoint)

    last_error: Exception | None = None
    for endpoint in ordered:
        payload: dict[str, Any] = {"model": OLLAMA_EMBED_MODEL}
        if (OLLAMA_KEEP_ALIVE or "").strip():
            payload["keep_alive"] = OLLAMA_KEEP_ALIVE.strip()
        if endpoint.endswith("/api/embed") or endpoint.endswith("/v1/embeddings"):
            payload["input"] = text
        else:
            payload["prompt"] = text
        try:
            response = requests.post(endpoint, json=payload, timeout=45)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            vector = _extract_embedding(response.json())
            if vector:
                return vector
        except Exception as exc:
            last_error = exc
            continue

    raise RuntimeError(f"Failed Ollama embedding request: {last_error}")


def get_collection_names(client: Any) -> set[str]:
    try:
        return {item.name for item in client.get_collections().collections}
    except Exception:
        return set()


def get_collection_vector_size(client: Any, collection_name: str) -> int | None:
    try:
        info = client.get_collection(collection_name)
    except Exception:
        return None
    try:
        vectors = info.config.params.vectors
        if isinstance(vectors, dict):
            for cfg in vectors.values():
                size = getattr(cfg, "size", None)
                if size:
                    return int(size)
            return None
        size = getattr(vectors, "size", None)
        if size:
            return int(size)
    except Exception:
        return None
    return None


def build_query_vector(query: str, target_size: int, model: Any | None) -> list[float]:
    try:
        vector = request_ollama_embedding(query)
        return normalize_embedding(vector, target_size)
    except Exception:
        pass

    if model is not None:
        try:
            vector = model.encode([query], show_progress_bar=False)[0].tolist()
            return normalize_embedding(vector, target_size)
        except Exception:
            pass

    return build_local_embedding(query, target_size)


def _search_with_qdrant(
    client: Any,
    collection_name: str,
    query_vector: list[float],
    limit: int,
    query_filter: Any | None = None,
) -> list[Any]:
    kwargs: dict[str, Any] = {
        "collection_name": collection_name,
        "query_vector": query_vector,
        "limit": limit,
    }
    if query_filter is not None:
        kwargs["query_filter"] = query_filter

    try:
        return list(client.search(**kwargs))
    except TypeError:
        if "query_filter" in kwargs:
            kwargs.pop("query_filter", None)
        return list(client.search(**kwargs))


def _hit_to_result(hit: Any, source: str) -> dict[str, Any]:
    payload = getattr(hit, "payload", {}) or {}
    node_id = str(payload.get("node_id") or payload.get("doc_id") or getattr(hit, "id", ""))
    summary = str(payload.get("summary") or payload.get("title") or "").strip()
    node_type = str(payload.get("node_type") or payload.get("record_type") or "memory").strip()
    source_project_slug = str(payload.get("source_project_slug") or payload.get("project_slug") or "").strip()

    source_files = payload.get("source_files") or []
    if isinstance(source_files, list) and source_files:
        default_path = str(source_files[0])
    else:
        default_path = ""
    relative_path = str(payload.get("relative_path") or default_path)
    details = str(payload.get("details") or payload.get("snippet") or "").strip()

    title = summary if summary else f"{node_type}:{node_id}"
    lexical_text = " ".join([title, relative_path, details, node_type, source_project_slug]).lower()
    return {
        "score": round(float(getattr(hit, "score", 0.0)), 4),
        "method": "semantic-memory",
        "id": node_id,
        "title": title,
        "path": relative_path,
        "snippet": details[:400],
        "source": source,
        "source_project_slug": source_project_slug,
        "_rank_score": float(getattr(hit, "score", 0.0)),
        "_lexical_text": lexical_text,
    }


def _tokenize_query(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9_]+", (query or "").lower())
    dedup: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if len(token) < 2:
            continue
        if token in seen:
            continue
        seen.add(token)
        dedup.append(token)
    return dedup


def _lexical_boost(query_tokens: list[str], lexical_text: str) -> float:
    if not query_tokens:
        return 0.0
    haystack = (lexical_text or "").lower()
    matches = 0
    for token in query_tokens:
        if token in haystack:
            matches += 1
    return float(matches) / float(len(query_tokens))


def merge_memory_results(
    query: str,
    project_results: list[dict[str, Any]],
    global_results: list[dict[str, Any]],
    top_k: int,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    query_tokens = _tokenize_query(query)

    for item in project_results:
        ranked = dict(item)
        semantic = float(item.get("_rank_score", 0.0)) * 1.05
        lexical = _lexical_boost(query_tokens, str(item.get("_lexical_text", "")))
        ranked["_rank_score"] = (semantic * 0.85) + (lexical * 0.15)
        key = f"{item.get('source_project_slug', '')}::{item.get('id', '')}"
        current = merged.get(key)
        if current is None or ranked["_rank_score"] > current.get("_rank_score", 0.0):
            merged[key] = ranked

    for item in global_results:
        ranked = dict(item)
        semantic = float(item.get("_rank_score", 0.0)) * 0.95
        lexical = _lexical_boost(query_tokens, str(item.get("_lexical_text", "")))
        ranked["_rank_score"] = (semantic * 0.85) + (lexical * 0.15)
        key = f"{item.get('source_project_slug', '')}::{item.get('id', '')}"
        current = merged.get(key)
        if current is None or ranked["_rank_score"] > current.get("_rank_score", 0.0):
            merged[key] = ranked

    ordered = sorted(merged.values(), key=lambda entry: entry.get("_rank_score", 0.0), reverse=True)[:top_k]
    for entry in ordered:
        entry.pop("_rank_score", None)
        entry.pop("_lexical_text", None)
    return ordered


def semantic_search_memory(
    query: str,
    top_k: int,
    project_slug: str,
    project_collection: str,
    global_collection: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    debug: dict[str, Any] = {
        "project_collection": project_collection,
        "global_collection": global_collection,
        "project_hits": 0,
        "global_hits": 0,
    }
    if not QDRANT_AVAILABLE:
        return [], {"reason": "qdrant-client-not-installed", **debug}

    try:
        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=5, check_compatibility=False)
    except TypeError:
        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=5)
    available = get_collection_names(client)
    if project_collection not in available and global_collection not in available:
        return [], {"reason": "memory-collections-not-found", **debug}

    model = get_sentence_transformer_model()
    project_results: list[dict[str, Any]] = []
    global_results: list[dict[str, Any]] = []

    if project_collection in available:
        project_dim = get_collection_vector_size(client, project_collection) or int(
            os.getenv("QDRANT_VECTOR_SIZE", "768")
        )
        project_query_vector = build_query_vector(query, project_dim, model)
        project_hits = _search_with_qdrant(
            client=client,
            collection_name=project_collection,
            query_vector=project_query_vector,
            limit=top_k,
            query_filter=None,
        )
        project_results = [_hit_to_result(hit, source="project-memory") for hit in project_hits]
        debug["project_hits"] = len(project_results)

    if global_collection in available:
        global_dim = get_collection_vector_size(client, global_collection) or int(
            os.getenv("QDRANT_VECTOR_SIZE", "768")
        )
        global_query_vector = build_query_vector(query, global_dim, model)
        global_filter = None
        if Filter is not None and FieldCondition is not None and MatchValue is not None:
            global_filter = Filter(
                must_not=[
                    FieldCondition(
                        key="source_project_slug",
                        match=MatchValue(value=project_slug),
                    )
                ]
            )
        global_hits = _search_with_qdrant(
            client=client,
            collection_name=global_collection,
            query_vector=global_query_vector,
            limit=top_k,
            query_filter=global_filter,
        )
        global_results = [_hit_to_result(hit, source="global-memory") for hit in global_hits]
        debug["global_hits"] = len(global_results)

    merged = merge_memory_results(query, project_results, global_results, top_k)
    return merged, debug


def semantic_search_docs(query: str, docs: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    model = get_sentence_transformer_model()
    if not QDRANT_AVAILABLE or model is None:
        return []

    try:
        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=5, check_compatibility=False)
    except TypeError:
        client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=5)

    existing = get_collection_names(client)
    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )

    existing_ids: set[str] = set()
    try:
        scroll_result = client.scroll(collection_name=COLLECTION_NAME, limit=10000, with_payload=False)
        existing_ids = {str(point.id) for point in scroll_result[0]}
    except Exception:
        existing_ids = set()

    to_index = [doc for doc in docs if doc["id"] not in existing_ids]
    if to_index:
        texts = [doc["text"][:2000] for doc in to_index]
        vectors = model.encode(texts, show_progress_bar=False).tolist()
        points = [
            PointStruct(
                id=abs(hash(doc["id"])) % (2**63),
                vector=vector,
                payload={
                    "doc_id": doc["id"],
                    "title": doc["title"],
                    "path": doc["relative_path"],
                    "snippet": doc["text"][:400],
                },
            )
            for doc, vector in zip(to_index, vectors)
        ]
        client.upsert(collection_name=COLLECTION_NAME, points=points)

    query_vec = model.encode([query], show_progress_bar=False)[0].tolist()
    hits = _search_with_qdrant(client, COLLECTION_NAME, query_vec, top_k, None)
    results: list[dict[str, Any]] = []
    for hit in hits:
        payload = getattr(hit, "payload", {}) or {}
        results.append(
            {
                "score": round(float(getattr(hit, "score", 0.0)), 4),
                "method": "semantic-docs",
                "id": payload.get("doc_id", ""),
                "title": payload.get("title", ""),
                "path": payload.get("path", ""),
                "snippet": payload.get("snippet", ""),
                "source": "docs-semantic",
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Search reusable lessons and patterns for orchestrator tasks.")
    parser.add_argument("--project-path", required=True, help="Project root path")
    parser.add_argument("--query", required=True, help="Error description or task objective")
    parser.add_argument("--top-k", type=int, default=5, help="Result limit (default: 5)")
    parser.add_argument("--force-keyword", action="store_true", help="Skip semantic search paths")
    args = parser.parse_args()

    project_path = Path(args.project_path).resolve()
    if not project_path.exists():
        print(json.dumps({"error": f"Project path not found: {project_path}"}))
        sys.exit(1)

    top_k = max(1, int(args.top_k))
    query = (args.query or "").strip()
    project_slug = derive_project_slug(project_path)
    project_collection = resolve_project_collection_name(project_slug)
    global_collection = (
        os.getenv("ORCHESTRATOR_GLOBAL_QDRANT_COLLECTION", GLOBAL_COLLECTION_DEFAULT).strip()
        or GLOBAL_COLLECTION_DEFAULT
    )

    docs = load_documents(project_path)
    results: list[dict[str, Any]] = []
    method = "none"
    note = ""
    memory_debug: dict[str, Any] = {}

    if not args.force_keyword:
        try:
            results, memory_debug = semantic_search_memory(
                query=query,
                top_k=top_k,
                project_slug=project_slug,
                project_collection=project_collection,
                global_collection=global_collection,
            )
            if results:
                method = "semantic-memory-hybrid"
        except Exception as exc:
            memory_debug = {"reason": "memory-search-failed", "error": str(exc)}

    if not results and not args.force_keyword and docs:
        try:
            results = semantic_search_docs(query, docs, top_k)
            if results:
                method = "semantic-docs"
        except Exception as exc:
            note = f"semantic-docs-fallback: {exc}"

    if not results and docs:
        results = keyword_search(query, docs, top_k)
        if results:
            method = "keyword"

    if not results and method == "none":
        if memory_debug:
            note = memory_debug.get("reason", "") or note
        if not note and not docs:
            note = "No memory hits and no lesson/pattern files found."

    output = {
        "query": query,
        "project_slug": project_slug,
        "method": method,
        "total_docs_searched": len(docs),
        "memory": memory_debug,
        "results": results,
    }
    if note:
        output["note"] = note

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
