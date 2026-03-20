from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import hashlib
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from services.semantic_backend import (
    embed_text as _shared_embed_text,
    ensure_collection as _shared_ensure_collection,
    list_collections as _shared_list_collections,
    scroll_points as _shared_scroll_points,
    upsert_point as _shared_upsert_point,
)
from services.streaming.core.db import async_db

log = logging.getLogger("memory_compaction")

COMPACTION_INTERVAL_S = int(env_get("ORCHESTRATOR_MEMORY_COMPACTION_INTERVAL_SECONDS", default="3600"))
COMPACTION_MIN_GROUP_SIZE = int(env_get("ORCHESTRATOR_MEMORY_COMPACTION_MIN_GROUP_SIZE", default="2"))
REACTIVATION_TTL_HOURS = int(env_get("ORCHESTRATOR_MEMORY_REACTIVATION_TTL_HOURS", default="168"))
MAX_COLLECTION_POINTS = int(env_get("ORCHESTRATOR_MEMORY_COMPACTION_MAX_POINTS", default="250"))
def _parse_timestamp(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _embed_text(text: str) -> tuple[list[float], str | None]:
    return _shared_embed_text(text, timeout=30)


def _ensure_qdrant_collection(collection: str, vector_size: int) -> str | None:
    return _shared_ensure_collection(collection, vector_size, timeout=20)


def _upsert_qdrant(collection: str, vector: list[float], payload: dict[str, Any]) -> str | None:
    _, error = _shared_upsert_point(collection, vector, payload, point_id=str(payload.get("id") or ""), timeout=20)
    return error


def _list_memory_collections() -> list[str]:
    names, error = _shared_list_collections(timeout=20)
    if error:
        raise RuntimeError(error)
    return [name for name in names if name.endswith("_agent_memory")]


def _scroll_collection(collection: str, limit: int = MAX_COLLECTION_POINTS) -> list[dict[str, Any]]:
    points, error = _shared_scroll_points(collection, limit=limit, with_payload=True, with_vector=False, timeout=30)
    if error:
        raise RuntimeError(error)
    return points


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8", errors="replace")).hexdigest()[:20]
    return f"mc-{digest}"


def _normalize_snippet(text: str, *, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _group_points(points: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    incident_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    file_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        payload = point.get("payload") or {}
        metadata = payload.get("metadata") or {}
        if payload.get("source") == "memory_compactor":
            continue
        incident_family = str(metadata.get("incident_family") or "").strip().lower()
        if incident_family and incident_family != "generic":
            incident_groups[incident_family].append(point)
        for file_path in list(metadata.get("files") or [])[:3]:
            normalized = str(file_path or "").strip()
            if normalized:
                file_groups[normalized].append(point)
    return incident_groups, file_groups


def _build_compacted_summary(
    *,
    collection: str,
    group_kind: str,
    group_key: str,
    items: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    payloads = [item.get("payload") or {} for item in items]
    metadatas = [payload.get("metadata") or {} for payload in payloads]
    statuses = Counter(str(meta.get("status") or "").strip().lower() or "unknown" for meta in metadatas)
    task_types = Counter(str(meta.get("task_type") or "").strip().lower() or "generic" for meta in metadatas)
    incident_family = (
        group_key if group_kind == "incident_family" else Counter(
            str(meta.get("incident_family") or "").strip().lower()
            for meta in metadatas
            if str(meta.get("incident_family") or "").strip()
        ).most_common(1)[0][0] if any(str(meta.get("incident_family") or "").strip() for meta in metadatas) else ""
    )
    files = [group_key] if group_kind == "file_path" else []
    for meta in metadatas:
        for file_path in list(meta.get("files") or [])[:3]:
            normalized = str(file_path or "").strip()
            if normalized and normalized not in files:
                files.append(normalized)

    snippets = []
    for payload in payloads[:3]:
        content = str(payload.get("content") or "").strip()
        if content:
            snippets.append(_normalize_snippet(content))

    summary_lines = [
        f"Compacted memory for {group_kind}={group_key}.",
        f"Signals: count={len(items)}, dominant_task_type={task_types.most_common(1)[0][0]}, dominant_status={statuses.most_common(1)[0][0]}.",
    ]
    if incident_family:
        summary_lines.append(f"Incident family: {incident_family}.")
    if files:
        summary_lines.append(f"Files: {', '.join(files[:3])}.")
    if snippets:
        summary_lines.append("Evidence:")
        for idx, snippet in enumerate(snippets, start=1):
            summary_lines.append(f"{idx}. {snippet}")

    recent_ts = max((_parse_timestamp(payload.get("timestamp")) for payload in payloads), default=None)
    metadata = {
        "collection": collection,
        "group_kind": group_kind,
        "group_key": group_key,
        "incident_family": incident_family,
        "files": files[:5],
        "compacted_count": len(items),
        "source_ids": [str(item.get("id") or "") for item in items[:10]],
        "task_type": task_types.most_common(1)[0][0] if task_types else "generic",
        "status": statuses.most_common(1)[0][0] if statuses else "unknown",
        "recent_timestamp": recent_ts.isoformat() if recent_ts else None,
    }
    return "\n".join(summary_lines), metadata


async def ensure_memory_compaction_schema() -> None:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_reactivation_hints (
                    hint_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    hint_kind TEXT NOT NULL DEFAULT 'incident_family',
                    task_type TEXT NOT NULL DEFAULT '',
                    incident_family TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    strength DOUBLE PRECISION NOT NULL DEFAULT 0,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ
                )
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_reactivation_hints_lookup
                    ON memory_reactivation_hints
                    (tenant_id, project_id, task_type, incident_family, file_path, updated_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_compaction_runs (
                    run_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    collection_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    compacted_entries INTEGER NOT NULL DEFAULT 0,
                    hints_upserted INTEGER NOT NULL DEFAULT 0,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                )
                """
            )
        await conn.commit()


async def _upsert_reactivation_hint(
    *,
    tenant_id: str,
    project_id: str,
    hint_kind: str,
    task_type: str,
    incident_family: str,
    file_path: str,
    summary: str,
    strength: float,
    metadata: dict[str, Any],
) -> None:
    hint_id = _stable_id(tenant_id, project_id, hint_kind, task_type or "-", incident_family or "-", file_path or "-")
    expires_at = datetime.now(timezone.utc) + timedelta(hours=REACTIVATION_TTL_HOURS)
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO memory_reactivation_hints
                    (hint_id, tenant_id, project_id, hint_kind, task_type, incident_family, file_path,
                     summary, strength, metadata, updated_at, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), %s)
                ON CONFLICT (hint_id) DO UPDATE
                   SET summary = EXCLUDED.summary,
                       strength = EXCLUDED.strength,
                       metadata = EXCLUDED.metadata,
                       updated_at = NOW(),
                       expires_at = EXCLUDED.expires_at
                """,
                (
                    hint_id,
                    tenant_id,
                    project_id,
                    hint_kind,
                    task_type,
                    incident_family,
                    file_path,
                    summary,
                    strength,
                    json.dumps(metadata),
                    expires_at,
                ),
            )
        await conn.commit()


def _hint_match_score(
    row: dict[str, Any],
    *,
    project_id: str = "",
    task_type: str = "",
    file_path: str = "",
    incident_family: str = "",
) -> int:
    score = 0

    def _boost(value: str, expected: str, *, exact: int, blank: int) -> int:
        normalized_value = str(value or "").strip()
        normalized_expected = str(expected or "").strip()
        if normalized_expected and normalized_value == normalized_expected:
            return exact
        if not normalized_value:
            return blank
        return 0

    score += _boost(row.get("project_id") or "", project_id, exact=4, blank=1)
    score += _boost(row.get("task_type") or "", task_type, exact=3, blank=1)
    score += _boost(row.get("incident_family") or "", incident_family, exact=4, blank=1)
    score += _boost(row.get("file_path") or "", file_path, exact=5, blank=1)

    metadata = row.get("metadata") or {}
    files = [str(path or "").strip() for path in (metadata.get("files") or []) if str(path or "").strip()]
    if file_path and file_path in files:
        score += 2
    source_ids = metadata.get("source_ids") or []
    if source_ids:
        score += min(len(source_ids), 5)
    score += int(float(row.get("strength") or 0.0) * 10)
    return score


async def fetch_reactivation_hints(
    *,
    tenant_id: str,
    project_id: str = "",
    task_type: str = "",
    file_path: str = "",
    incident_family: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    await ensure_memory_compaction_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT hint_id, project_id, hint_kind, task_type, incident_family, file_path, summary, strength, metadata, updated_at
                  FROM memory_reactivation_hints
                 WHERE tenant_id = %s
                   AND (expires_at IS NULL OR expires_at > NOW())
                   AND (%s = '' OR project_id = %s OR project_id = '')
                   AND (%s = '' OR task_type = %s OR task_type = '')
                   AND (%s = '' OR file_path = %s OR file_path = '')
                   AND (%s = '' OR incident_family = %s OR incident_family = '')
                 LIMIT %s
                """,
                (
                    tenant_id,
                    project_id,
                    project_id,
                    task_type,
                    task_type,
                    file_path,
                    file_path,
                    incident_family,
                    incident_family,
                    max(limit * 6, 20),
                ),
            )
            rows = [dict(row) for row in await cur.fetchall()]
    for row in rows:
        row["match_score"] = _hint_match_score(
            row,
            project_id=project_id,
            task_type=task_type,
            file_path=file_path,
            incident_family=incident_family,
        )
    rows.sort(
        key=lambda row: (
            int(row.get("match_score") or 0),
            float(row.get("strength") or 0.0),
            str(row.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return rows[:limit]


async def _record_compaction_run(
    *,
    tenant_id: str,
    project_id: str,
    collection_name: str,
    status: str,
    compacted_entries: int,
    hints_upserted: int,
    metadata: dict[str, Any],
) -> None:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO memory_compaction_runs
                    (tenant_id, project_id, collection_name, status, compacted_entries, hints_upserted, metadata, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                """,
                (
                    tenant_id,
                    project_id,
                    collection_name,
                    status,
                    compacted_entries,
                    hints_upserted,
                    json.dumps(metadata),
                ),
            )
        await conn.commit()


async def compact_memory_once() -> dict[str, Any]:
    await ensure_memory_compaction_schema()
    try:
        collections = await asyncio.to_thread(_list_memory_collections)
    except Exception as exc:
        log.warning("memory_compaction_collection_discovery_failed error=%s", exc)
        return {"collections": 0, "compacted": 0, "reactivated": 0, "status": "failed", "error": str(exc)}

    compacted = 0
    reactivated = 0
    processed_collections = 0

    for collection in collections:
        try:
            points = await asyncio.to_thread(_scroll_collection, collection, MAX_COLLECTION_POINTS)
        except Exception as exc:
            log.warning("memory_compaction_scroll_failed collection=%s error=%s", collection, exc)
            continue
        if not points:
            continue

        sample_payload = (points[0].get("payload") or {})
        tenant_id = str(sample_payload.get("tenant_id") or "local")
        project_id = str(sample_payload.get("project_id") or "sinc")

        incident_groups, file_groups = _group_points(points)
        collection_compacted = 0
        collection_reactivated = 0

        grouped = []
        grouped.extend(("incident_family", key, items) for key, items in incident_groups.items())
        grouped.extend(("file_path", key, items) for key, items in file_groups.items())

        for group_kind, group_key, items in grouped:
            if len(items) < COMPACTION_MIN_GROUP_SIZE:
                continue
            summary, metadata = _build_compacted_summary(
                collection=collection,
                group_kind=group_kind,
                group_key=group_key,
                items=items,
            )
            vector, error = await asyncio.to_thread(_embed_text, summary)
            if error:
                log.warning("memory_compaction_embed_failed collection=%s key=%s error=%s", collection, group_key, error)
                continue
            payload = {
                "id": _stable_id(collection, group_kind, group_key),
                "content": summary,
                "tags": [
                    "memory_compaction",
                    f"group:{group_kind}",
                    f"task_type:{metadata.get('task_type') or 'generic'}",
                    *( [f"incident:{metadata.get('incident_family')}"] if metadata.get("incident_family") else [] ),
                    *( [f"file:{metadata['files'][0]}"] if metadata.get("files") else [] ),
                ],
                "tenant_id": tenant_id,
                "project_id": project_id,
                "source": "memory_compactor",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata,
            }
            upsert_error = await asyncio.to_thread(_upsert_qdrant, collection, vector, payload)
            if upsert_error:
                log.warning("memory_compaction_upsert_failed collection=%s key=%s error=%s", collection, group_key, upsert_error)
                continue

            strength = min(1.0, 0.35 + (len(items) * 0.1))
            await _upsert_reactivation_hint(
                tenant_id=tenant_id,
                project_id=project_id,
                hint_kind=group_kind,
                task_type=str(metadata.get("task_type") or ""),
                incident_family=str(metadata.get("incident_family") or ""),
                file_path=str(metadata.get("files", [""])[0] if metadata.get("files") else ""),
                summary=summary,
                strength=strength,
                metadata=metadata,
            )
            collection_compacted += 1
            collection_reactivated += 1

        await _record_compaction_run(
            tenant_id=tenant_id,
            project_id=project_id,
            collection_name=collection,
            status="completed",
            compacted_entries=collection_compacted,
            hints_upserted=collection_reactivated,
            metadata={"points_scanned": len(points)},
        )
        compacted += collection_compacted
        reactivated += collection_reactivated
        processed_collections += 1

    return {
        "collections": processed_collections,
        "compacted": compacted,
        "reactivated": reactivated,
        "status": "ok",
    }


async def run_memory_compaction_loop() -> None:
    log.info(
        "starting_memory_compaction_worker interval_s=%s min_group_size=%s ttl_hours=%s",
        COMPACTION_INTERVAL_S,
        COMPACTION_MIN_GROUP_SIZE,
        REACTIVATION_TTL_HOURS,
    )
    while True:
        try:
            summary = await compact_memory_once()
            log.info(
                "memory_compaction_cycle_done collections=%s compacted=%s reactivated=%s status=%s",
                summary.get("collections"),
                summary.get("compacted"),
                summary.get("reactivated"),
                summary.get("status"),
            )
        except Exception as exc:
            log.warning("memory_compaction_cycle_failed error=%s", exc)
        await asyncio.sleep(COMPACTION_INTERVAL_S)
