from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from services.entropy_scanner import EntropyScanner
from services.otel_setup import bootstrap_worker_otel
from services.streaming.core.db import async_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("entropy_worker")

SCAN_INTERVAL_S = int(env_get("ORCHESTRATOR_ENTROPY_INTERVAL_SECONDS", default="21600"))
SEED_THRESHOLD = float(env_get("ORCHESTRATOR_ENTROPY_SEED_THRESHOLD", default="0.70"))


def _sync_db_conn():
    from services.streaming.core.db import db
    return db(bypass_rls=True)


async def _discover_project_paths() -> list[dict[str, str]]:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (COALESCE(tenant_id, 'local'), project_id)
                       project_id,
                       COALESCE(tenant_id, 'local') AS tenant_id,
                       project_path
                FROM ingest_pipelines
                WHERE project_id IS NOT NULL
                  AND COALESCE(project_path, '') <> ''
                  AND status IN ('done', 'completed', 'success', 'running')
                ORDER BY COALESCE(tenant_id, 'local'),
                         project_id,
                         COALESCE(completed_at, updated_at, requested_at) DESC
                """
            )
            rows = await cur.fetchall()

    discovered: list[dict[str, str]] = []
    for row in rows:
        project_path = str(row.get("project_path") or "").strip()
        if not project_path:
            continue
        normalized = str(Path(project_path))
        if not os.path.isdir(normalized):
            continue
        discovered.append(
            {
                "project_id": str(row.get("project_id") or ""),
                "tenant_id": str(row.get("tenant_id") or "local"),
                "project_path": normalized,
            }
        )
    return discovered


async def entropy_tick_once() -> dict[str, Any]:
    scanner = EntropyScanner(db_conn_fn=_sync_db_conn)
    projects = await _discover_project_paths()
    scanned = 0
    seeded = 0

    for project in projects:
        project_path = project["project_path"]
        project_id = project["project_id"]
        tenant_id = project["tenant_id"]
        try:
            summary = await asyncio.to_thread(scanner.scan_and_store, project_path, project_id, tenant_id)
            created = await asyncio.to_thread(scanner.seed_tasks, project_id, tenant_id, threshold=SEED_THRESHOLD)
            scanned += int(summary.get("files_scanned", 0))
            seeded += len(created or [])
            log.info(
                "entropy_worker_scan_complete tenant=%s project=%s files=%s seeded=%s",
                tenant_id,
                project_id,
                summary.get("files_scanned", 0),
                len(created or []),
            )
        except Exception as exc:
            log.warning(
                "entropy_worker_scan_failed tenant=%s project=%s path=%s error=%s",
                tenant_id,
                project_id,
                project_path,
                exc,
            )

    return {"projects": len(projects), "files_scanned": scanned, "seeded": seeded}


async def run_entropy_loop() -> None:
    bootstrap_worker_otel("orchestrator-entropy")
    log.info("starting_entropy_worker interval_s=%s seed_threshold=%s", SCAN_INTERVAL_S, SEED_THRESHOLD)
    while True:
        await entropy_tick_once()
        await asyncio.sleep(SCAN_INTERVAL_S)


if __name__ == "__main__":
    asyncio.run(run_entropy_loop())
