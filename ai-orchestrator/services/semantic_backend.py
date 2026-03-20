from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
from services.http_client import create_sync_resilient_client
from services.streaming.core.config import env_get


def qdrant_url() -> str:
    host = env_get("QDRANT_HOST", default="qdrant")
    port = int(env_get("QDRANT_PORT", default="6333"))
    return f"http://{host}:{port}"


def ollama_url() -> str:
    return env_get("OLLAMA_HOST", default="http://ollama:11434").rstrip("/")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: int = 20,
    service_name: str = "semantic-backend",
) -> tuple[dict[str, Any], str | None]:
    try:
        with create_sync_resilient_client(service_name=service_name, timeout=timeout) as client:
            response = client.request(method, url, json=body)
            response.raise_for_status()
            if not response.text.strip():
                return {}, None
            payload = response.json()
            return payload if isinstance(payload, dict) else {"result": payload}, None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {}, "404:not_found"
        text = exc.response.text[:400] if exc.response is not None else ""
        return {}, f"{exc.response.status_code}:{text or exc.__class__.__name__}"
    except Exception as exc:
        return {}, str(exc)


def embed_text(text: str, *, model: str | None = None, timeout: int = 30) -> tuple[list[float], str | None]:
    embed_model = model or env_get("OLLAMA_EMBED_MODEL", default="nomic-embed-text")
    payload, error = _request_json(
        f"{ollama_url()}/api/embeddings",
        method="POST",
        body={"model": embed_model, "prompt": text},
        timeout=timeout,
        service_name="semantic-backend-ollama",
    )
    if error:
        return [], error
    vector = payload.get("embedding", [])
    if not vector:
        return [], "embedding returned empty vector"
    return vector, None


def ensure_collection(collection: str, vector_size: int, *, timeout: int = 20) -> str | None:
    _, error = _request_json(
        f"{qdrant_url()}/collections/{collection}",
        method="PUT",
        body={"vectors": {"size": vector_size, "distance": "Cosine"}},
        timeout=timeout,
        service_name="semantic-backend-qdrant",
    )
    return error


def search_points(
    collection: str,
    vector: list[float],
    top_k: int,
    *,
    filters: dict[str, Any] | None = None,
    sparse_vector: dict[str, Any] | None = None,
    timeout: int = 20,
) -> tuple[list[dict[str, Any]], str | None]:
    if not vector:
        return [], None
    body: dict[str, Any] = {"vector": vector, "limit": top_k, "with_payload": True}
    if filters:
        body["filter"] = filters
    if sparse_vector:
        body["params"] = {"hnsw_ef": 128, "exact": False}
        body["prefetch"] = [
            {"vector": vector, "limit": top_k},
            {"vector": {"name": "sparse-text", "vector": sparse_vector}, "limit": top_k},
        ]
        if isinstance(sparse_vector, dict) and "indices" in sparse_vector:
            body["sparse_vector"] = {"name": "sparse-text", "vector": sparse_vector}
    payload, error = _request_json(
        f"{qdrant_url()}/collections/{collection}/points/search",
        method="POST",
        body=body,
        timeout=timeout,
        service_name="semantic-backend-qdrant",
    )
    if error == "404:not_found":
        return [], None
    if error:
        return [], error
    return payload.get("result", []), None


def upsert_point(
    collection: str,
    vector: list[float],
    payload: dict[str, Any],
    *,
    point_id: str | None = None,
    timeout: int = 20,
) -> tuple[str, str | None]:
    actual_id = point_id or str(payload.get("id") or uuid4())
    body = {
        "points": [
            {
                "id": actual_id,
                "vector": vector,
                "payload": payload,
            }
        ]
    }
    _, error = _request_json(
        f"{qdrant_url()}/collections/{collection}/points?wait=true",
        method="PUT",
        body=body,
        timeout=timeout,
        service_name="semantic-backend-qdrant",
    )
    if error == "404:not_found":
        created = ensure_collection(collection, len(vector), timeout=timeout)
        if created:
            return actual_id, created
        _, retry_error = _request_json(
            f"{qdrant_url()}/collections/{collection}/points?wait=true",
            method="PUT",
            body=body,
            timeout=timeout,
            service_name="semantic-backend-qdrant",
        )
        return actual_id, retry_error
    return actual_id, error


def list_collections(*, timeout: int = 20) -> tuple[list[str], str | None]:
    payload, error = _request_json(
        f"{qdrant_url()}/collections",
        method="GET",
        timeout=timeout,
        service_name="semantic-backend-qdrant",
    )
    if error:
        return [], error
    collections = payload.get("result", {}).get("collections", [])
    return [str(item.get("name") or "").strip() for item in collections if str(item.get("name") or "").strip()], None


def scroll_points(
    collection: str,
    *,
    limit: int,
    with_payload: bool = True,
    with_vector: bool = False,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], str | None]:
    payload, error = _request_json(
        f"{qdrant_url()}/collections/{collection}/points/scroll",
        method="POST",
        body={
            "limit": limit,
            "with_payload": with_payload,
            "with_vector": with_vector,
        },
        timeout=timeout,
        service_name="semantic-backend-qdrant",
    )
    if error:
        return [], error
    return payload.get("result", {}).get("points", []), None
