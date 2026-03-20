from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import contextlib
import json
import logging
import os
import psycopg
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.dynamic_rules import DynamicRuleEngine

from .auth import now_iso
from .config import DB_CONFIG
from .db import async_db, db
from .runtime_plane import compute_readiness_snapshot, ensure_runtime_plane_schema
from .state_plane import get_system_status_snapshot

log = logging.getLogger("orchestrator.governance")

POLICY_INTERVAL_S = int(env_get("ORCHESTRATOR_POLICY_INTERVAL_SECONDS", default="120"))
MUTATION_INTERVAL_S = int(env_get("ORCHESTRATOR_MUTATION_INTERVAL_SECONDS", default="300"))
FINOPS_INTERVAL_S = int(env_get("ORCHESTRATOR_FINOPS_INTERVAL_SECONDS", default="180"))
DEPLOY_VERIFY_INTERVAL_S = int(env_get("ORCHESTRATOR_DEPLOY_VERIFY_INTERVAL_SECONDS", default="180"))
RELEASE_INTERVAL_S = int(env_get("ORCHESTRATOR_RELEASE_INTERVAL_SECONDS", default="300"))
PATTERN_PROMOTION_INTERVAL_S = int(env_get("ORCHESTRATOR_PATTERN_PROMOTION_INTERVAL_SECONDS", default="300"))

MUTATION_COMMAND = env_get("ORCHESTRATOR_MUTATION_COMMAND", default="").strip()
MUTATION_TIMEOUT_S = int(env_get("ORCHESTRATOR_MUTATION_TIMEOUT_SECONDS", default="1800"))
MUTATION_REQUIRED = env_get("ORCHESTRATOR_MUTATION_REQUIRED", default="0") == "1"
RELEASE_ALLOW_NO_MUTATION = env_get("ORCHESTRATOR_RELEASE_ALLOW_NO_MUTATION", default="0") == "1"
FINOPS_DISK_FREE_MIN_PERCENT = float(env_get("ORCHESTRATOR_FINOPS_DISK_FREE_MIN_PERCENT", default="10"))
FINOPS_MEM_FREE_MIN_MB = int(env_get("ORCHESTRATOR_FINOPS_MEM_FREE_MIN_MB", default="512"))


async def ensure_governance_schema() -> None:
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute("ALTER TABLE policy_reports ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''")
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS dynamic_rules (
                    rule_id TEXT PRIMARY KEY,
                    condition JSONB NOT NULL,
                    action TEXT NOT NULL,
                    confidence FLOAT NOT NULL DEFAULT 0.0,
                    created_from TEXT NOT NULL DEFAULT 'event_pattern',
                    times_applied INTEGER NOT NULL DEFAULT 0,
                    tenant_id TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dynamic_rules_tenant
                    ON dynamic_rules (tenant_id)
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_dynamic_rules_confidence
                    ON dynamic_rules (confidence DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS mutation_reports (
                    mutation_report_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    command TEXT NOT NULL DEFAULT '',
                    exit_code INTEGER,
                    summary TEXT NOT NULL DEFAULT '',
                    report JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mutation_reports_tenant_created
                    ON mutation_reports (tenant_id, created_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS finops_reports (
                    finops_report_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    summary TEXT NOT NULL DEFAULT '',
                    report JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_finops_reports_tenant_created
                    ON finops_reports (tenant_id, created_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS deploy_reports (
                    deploy_report_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    summary TEXT NOT NULL DEFAULT '',
                    report JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_deploy_reports_tenant_created
                    ON deploy_reports (tenant_id, created_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS release_reports (
                    release_report_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    blockers INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    report JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_release_reports_tenant_created
                    ON release_reports (tenant_id, created_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pattern_promotion_reports (
                    pattern_promotion_report_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    promoted_rules INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    report JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pattern_promotion_reports_tenant_created
                    ON pattern_promotion_reports (tenant_id, created_at DESC)
                """
            )
        await conn.commit()


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@contextlib.contextmanager
def _direct_db():
    with db(bypass_rls=True) as conn:
        yield conn


async def _store_loop_state(
    *,
    tenant_id: str,
    project_id: str,
    phase: str,
    status: str,
    summary: str,
    metadata: dict[str, Any],
) -> None:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO loop_states
                    (tenant_id, project_id, cycle, phase, status, summary, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (tenant_id, project_id) DO UPDATE SET
                    cycle = EXCLUDED.cycle,
                    phase = EXCLUDED.phase,
                    status = EXCLUDED.status,
                    summary = EXCLUDED.summary,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                (
                    tenant_id,
                    project_id,
                    int(datetime.now(timezone.utc).timestamp()),
                    phase,
                    status,
                    summary,
                    json.dumps(_json_safe(metadata)),
                ),
            )
        await conn.commit()


async def _insert_report(
    table: str,
    *,
    tenant_id: str,
    project_id: str,
    status: str,
    summary: str,
    report: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    extra = extra or {}
    cols = ["tenant_id", "project_id", "status", "summary", "report"]
    vals: list[Any] = [tenant_id, project_id, status, summary, json.dumps(_json_safe(report))]
    if table == "mutation_reports":
        cols.extend(["command", "exit_code"])
        vals.extend([extra.get("command", ""), extra.get("exit_code")])
    elif table == "release_reports":
        cols.append("blockers")
        vals.append(int(extra.get("blockers", 0)))
    elif table == "pattern_promotion_reports":
        cols.append("promoted_rules")
        vals.append(int(extra.get("promoted_rules", 0)))
    placeholders = ", ".join(["%s"] * len(cols))
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(vals),
            )
        await conn.commit()


async def _latest_report(table: str, tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_governance_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            query = f"SELECT * FROM {table} WHERE tenant_id = %s"
            params: list[Any] = [tenant_id]
            if project_id:
                query += " AND project_id = %s"
                params.append(project_id)
            query += " ORDER BY created_at DESC LIMIT 1"
            await cur.execute(query, tuple(params))
            row = await cur.fetchone()
    return dict(row) if row else {}


async def policy_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_governance_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'blocked-phase-approval') AS blocked_phase,
                    COUNT(*) FILTER (
                        WHERE status NOT IN ('done', 'cancelled')
                          AND (
                              COALESCE(title, '') ILIKE '%%security%%'
                              OR COALESCE(description, '') ILIKE '%%security%%'
                              OR COALESCE(title, '') ILIKE '%%deploy%%'
                              OR COALESCE(description, '') ILIKE '%%deploy%%'
                              OR COALESCE(title, '') ILIKE '%%release%%'
                              OR COALESCE(description, '') ILIKE '%%release%%'
                              OR COALESCE(title, '') ILIKE '%%migration%%'
                              OR COALESCE(description, '') ILIKE '%%migration%%'
                          )
                    ) AS high_risk_open
                FROM tasks
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            row = await cur.fetchone() or {}
    blocked_phase = int(row.get("blocked_phase") or 0)
    high_risk_open = int(row.get("high_risk_open") or 0)
    violations = blocked_phase
    status = "ok" if violations == 0 else "needs-review"
    summary = f"violations={violations} high_risk_open={high_risk_open}"
    report = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "status": status,
        "violations": violations,
        "blocked_phase_approval": blocked_phase,
        "high_risk_open_tasks": high_risk_open,
        "ts": now_iso(),
        "source": "python-governance",
    }
    await _insert_report(
        "policy_reports",
        tenant_id=tenant_id,
        project_id=project_id,
        status=status,
        summary=summary,
        report=report,
    )
    await _store_loop_state(
        tenant_id=tenant_id,
        project_id=project_id,
        phase="policy",
        status=status,
        summary=summary,
        metadata={"policy": report},
    )
    return {"status": "ok", "report": report, "ts": now_iso()}


async def mutation_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_governance_schema()
    failed_like = 0
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS failed_like
                FROM tasks
                WHERE tenant_id = %s
                  AND status IN ('failed', 'needs-revision', 'dead-letter')
                """,
                (tenant_id,),
            )
            failed_like = int((await cur.fetchone() or {}).get("failed_like") or 0)

    exit_code: int | None = None
    command = MUTATION_COMMAND
    stdout_tail = ""
    stderr_tail = ""
    if command:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=MUTATION_TIMEOUT_S)
            exit_code = int(proc.returncode or 0)
            stdout_tail = stdout.decode(errors="replace")[-4000:]
            stderr_tail = stderr.decode(errors="replace")[-4000:]
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            exit_code = 124
            stderr_tail = f"timeout after {MUTATION_TIMEOUT_S}s"
    else:
        exit_code = None

    if command and exit_code == 0:
        report_status = "ok"
    elif command:
        report_status = "failed"
    else:
        report_status = "not-configured"

    summary = f"status={report_status} failed_like={failed_like}"
    report = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "status": report_status,
        "command": command,
        "exit_code": exit_code,
        "failed_like_tasks": failed_like,
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "mutation_required": MUTATION_REQUIRED,
        "ts": now_iso(),
        "source": "python-governance",
    }
    await _insert_report(
        "mutation_reports",
        tenant_id=tenant_id,
        project_id=project_id,
        status=report_status,
        summary=summary,
        report=report,
        extra={"command": command, "exit_code": exit_code},
    )
    await _store_loop_state(
        tenant_id=tenant_id,
        project_id=project_id,
        phase="mutation",
        status=report_status,
        summary=summary,
        metadata={"mutation": report},
    )
    return {"status": "ok", "report": report, "ts": now_iso()}


def _memory_available_mb() -> int | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0])
        except ValueError:
            continue
    if "MemAvailable" in values:
        return int(values["MemAvailable"] / 1024)
    return None


async def finops_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_governance_schema()
    disk = shutil.disk_usage("/")
    disk_free_percent = round((disk.free / max(disk.total, 1)) * 100, 2)
    mem_available_mb = _memory_available_mb()
    readiness = await compute_readiness_snapshot(tenant_id)
    warnings: list[str] = []
    if disk_free_percent < FINOPS_DISK_FREE_MIN_PERCENT:
        warnings.append("disk-low")
    if mem_available_mb is not None and mem_available_mb < FINOPS_MEM_FREE_MIN_MB:
        warnings.append("memory-low")
    if int(readiness["counts"]["open_incidents"]) > 0:
        warnings.append("open-incidents")
    if int(readiness["counts"]["open_repairs"]) > 0:
        warnings.append("open-repairs")
    report_status = "ok" if not warnings else ("critical" if {"disk-low", "memory-low"} & set(warnings) else "warning")
    summary = f"status={report_status} warnings={','.join(warnings) if warnings else 'none'}"
    report = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "status": report_status,
        "disk_free_percent": disk_free_percent,
        "disk_free_bytes": disk.free,
        "memory_available_mb": mem_available_mb,
        "warnings": warnings,
        "readiness": readiness,
        "ts": now_iso(),
        "source": "python-governance",
    }
    await _insert_report(
        "finops_reports",
        tenant_id=tenant_id,
        project_id=project_id,
        status=report_status,
        summary=summary,
        report=report,
    )
    await _store_loop_state(
        tenant_id=tenant_id,
        project_id=project_id,
        phase="finops",
        status=report_status,
        summary=summary,
        metadata={"finops": report},
    )
    return {"status": "ok", "report": report, "ts": now_iso()}


async def deploy_verify_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_governance_schema()
    readiness = await compute_readiness_snapshot(tenant_id)
    system = await get_system_status_snapshot(tenant_id)
    blockers: list[str] = []
    if readiness["status"] == "not_ready":
        blockers.append("runtime-not-ready")
    if str(system.get("source", "")).lower() != "db":
        blockers.append("system-not-db-first")
    report_status = "ok" if not blockers else "failed"
    summary = f"status={report_status} blockers={','.join(blockers) if blockers else 'none'}"
    report = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "status": report_status,
        "blockers": blockers,
        "readiness": readiness,
        "system": system,
        "ts": now_iso(),
        "source": "python-governance",
    }
    await _insert_report(
        "deploy_reports",
        tenant_id=tenant_id,
        project_id=project_id,
        status=report_status,
        summary=summary,
        report=report,
    )
    await _store_loop_state(
        tenant_id=tenant_id,
        project_id=project_id,
        phase="deploy-verify",
        status=report_status,
        summary=summary,
        metadata={"deploy_verify": report},
    )
    return {"status": "ok", "report": report, "ts": now_iso()}


async def pattern_promotion_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_governance_schema()

    def _run_rule_learning() -> dict[str, Any]:
        engine = DynamicRuleEngine(_direct_db)
        engine.load_from_db(tenant_id=tenant_id)
        before = len(engine.get_all_rules())
        engine.learn_rules_from_history(tenant_id=tenant_id)
        after = len(engine.get_all_rules())
        return {"before": before, "after": after, "promoted_rules": max(after - before, 0)}

    result = await asyncio.to_thread(_run_rule_learning)
    report_status = "ok"
    summary = f"promoted_rules={result['promoted_rules']}"
    report = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "status": report_status,
        **result,
        "ts": now_iso(),
        "source": "python-governance",
    }
    await _insert_report(
        "pattern_promotion_reports",
        tenant_id=tenant_id,
        project_id=project_id,
        status=report_status,
        summary=summary,
        report=report,
        extra={"promoted_rules": result["promoted_rules"]},
    )
    await _store_loop_state(
        tenant_id=tenant_id,
        project_id=project_id,
        phase="pattern-promotion",
        status=report_status,
        summary=summary,
        metadata={"pattern_promotion": report},
    )
    return {"status": "ok", "report": report, "ts": now_iso()}


async def release_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_governance_schema()
    readiness = await compute_readiness_snapshot(tenant_id)
    policy = await _latest_report("policy_reports", tenant_id, project_id)
    mutation = await _latest_report("mutation_reports", tenant_id, project_id)
    finops = await _latest_report("finops_reports", tenant_id, project_id)
    deploy = await _latest_report("deploy_reports", tenant_id, project_id)
    blockers: list[str] = []

    policy_status = str(policy.get("status") or "unknown")
    policy_violations = int(policy.get("violations") or 0)
    mutation_status = str(mutation.get("status") or "unknown")
    finops_status = str(finops.get("status") or "unknown")
    deploy_status = str(deploy.get("status") or "unknown")

    if readiness["status"] == "not_ready":
        blockers.append("runtime-not-ready")
    if policy_status not in ("ok", "unknown") or policy_violations > 0:
        blockers.append("policy")
    if deploy_status not in ("ok", "unknown"):
        blockers.append("deploy-verify")
    if finops_status == "critical":
        blockers.append("finops")
    if mutation_status not in ("ok", "unknown") and not (
        RELEASE_ALLOW_NO_MUTATION and mutation_status == "not-configured"
    ):
        blockers.append("mutation")
    if mutation_status == "not-configured" and MUTATION_REQUIRED and not RELEASE_ALLOW_NO_MUTATION:
        blockers.append("mutation-required")

    report_status = "ready" if not blockers else "blocked"
    summary = f"status={report_status} blockers={','.join(blockers) if blockers else 'none'}"
    report = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "status": report_status,
        "blockers": blockers,
        "readiness": readiness,
        "policy": policy,
        "mutation": mutation,
        "finops": finops,
        "deploy_verify": deploy,
        "ts": now_iso(),
        "source": "python-governance",
    }
    await _insert_report(
        "release_reports",
        tenant_id=tenant_id,
        project_id=project_id,
        status=report_status,
        summary=summary,
        report=report,
        extra={"blockers": len(blockers)},
    )
    await _store_loop_state(
        tenant_id=tenant_id,
        project_id=project_id,
        phase="release",
        status=report_status,
        summary=summary,
        metadata={"release": report},
    )
    return {"status": "ok", "report": report, "ts": now_iso()}


async def get_latest_governance_snapshot(kind: str, tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    table_map = {
        "policy": "policy_reports",
        "mutation": "mutation_reports",
        "finops": "finops_reports",
        "deploy": "deploy_reports",
        "release": "release_reports",
        "pattern_promotion": "pattern_promotion_reports",
    }
    table = table_map[kind]
    return await _latest_report(table, tenant_id, project_id)


async def _loop(
    label: str,
    interval_s: int,
    tick,
    tenant_id: str = "local",
    project_id: str = "",
) -> None:
    log.info("%s_started tenant=%s interval=%s", label, tenant_id, interval_s)
    while True:
        try:
            await tick(tenant_id=tenant_id, project_id=project_id)
        except Exception as exc:
            log.exception("%s_error error=%s", label, exc)
        await asyncio.sleep(interval_s)


async def run_policy_loop(tenant_id: str = "local", project_id: str = "") -> None:
    await _loop("policy_worker", POLICY_INTERVAL_S, policy_tick_once, tenant_id, project_id)


async def run_mutation_loop(tenant_id: str = "local", project_id: str = "") -> None:
    await _loop("mutation_worker", MUTATION_INTERVAL_S, mutation_tick_once, tenant_id, project_id)


async def run_finops_loop(tenant_id: str = "local", project_id: str = "") -> None:
    await _loop("finops_worker", FINOPS_INTERVAL_S, finops_tick_once, tenant_id, project_id)


async def run_deploy_verify_loop(tenant_id: str = "local", project_id: str = "") -> None:
    await _loop("deploy_verify_worker", DEPLOY_VERIFY_INTERVAL_S, deploy_verify_tick_once, tenant_id, project_id)


async def run_pattern_promotion_loop(tenant_id: str = "local", project_id: str = "") -> None:
    await _loop("pattern_promotion_worker", PATTERN_PROMOTION_INTERVAL_S, pattern_promotion_tick_once, tenant_id, project_id)


async def run_release_loop(tenant_id: str = "local", project_id: str = "") -> None:
    await _loop("release_worker", RELEASE_INTERVAL_S, release_tick_once, tenant_id, project_id)
