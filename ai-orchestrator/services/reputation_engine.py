import asyncio
import json
import logging
import re
from typing import Any

from services.background_tasks import get_background_task_registry
from services.event_bus import get_event_bus
from services.streaming.core.db import async_db
from services.streaming.core.redis_ import (
    async_get_agent_reputation_score,
    async_update_agent_leaderboard,
    async_update_agent_reputation_hash,
    get_async_redis,
)
from services.streaming.core.schema_compat import get_table_columns_cached

log = logging.getLogger("orch.reputation")

_AFFINITY_PATTERNS: list[tuple[str, str]] = [
    (r"\b(api|endpoint|flask|fastapi|django|rest|graphql|grpc|service|backend)\b", "backend_affinity"),
    (r"\b(react|vue|angular|css|html|ui|ux|frontend|component|tailwind)\b", "frontend_affinity"),
    (r"\b(sql|postgres|mysql|migration|schema|index|query|database|db)\b", "db_affinity"),
    (r"\b(architecture|design|pattern|refactor|abstraction|interface|solid)\b", "arch_affinity"),
    (r"\b(test|pytest|unittest|coverage|mock|fixture|qa|spec|assert)\b", "qa_affinity"),
    (r"\b(docker|k8s|kubernetes|deploy|ci|cd|pipeline|infra|terraform|helm)\b", "devops_affinity"),
]


def _is_success(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized in {"done", "success", "completed", "complete"}


def _infer_affinity_column(task_type: str, task_title: str, summary: str) -> str | None:
    text = " ".join(part for part in (task_type, task_title, summary) if part).lower()
    for pattern, column in _AFFINITY_PATTERNS:
        if re.search(pattern, text):
            return column
    return None


class ReputationEngine:
    """
    Canonical reputation writer.

    Completion routes only publish audit events. This worker consumes the audit
    stream and updates:
      - Redis leaderboard and EMA hash
      - durable agent_reputation rows in Postgres
      - graph intelligence synchronization
    """

    def __init__(self, tenant_id: str = "local"):
        self.tenant_id = tenant_id
        self.stream_name = "sinc:stream:audit"
        self.group_name = "reputation_processor"
        self.consumer_name = "reputation_worker"
        self._shutdown = asyncio.Event()
        self._task_owner = f"reputation_engine:{tenant_id}:{id(self)}"
        self._background_tasks = get_background_task_registry()

    def _spawn_background_task(self, coro, *, name: str) -> asyncio.Task[Any]:
        return self._background_tasks.spawn(self._task_owner, coro, name=name)

    async def stop(self) -> None:
        self._shutdown.set()
        await self._background_tasks.cancel_owner(self._task_owner)

    def _resolve_event_tenant_id(self, data: dict[str, Any]) -> str:
        event_tenant = str(data.get("tenant_id") or "").strip()
        if event_tenant:
            return event_tenant
        configured = str(self.tenant_id or "").strip()
        if configured and configured != "local":
            return configured
        raise ValueError("missing_tenant_id")

    async def start(self):
        bus = await get_event_bus()
        await bus.create_consumer_group(self.stream_name, self.group_name)
        log.info("starting_reputation_engine stream=%s group=%s", self.stream_name, self.group_name)

        self._spawn_background_task(self._periodic_gds_update(), name="reputation.periodic_gds")

        try:
            while not self._shutdown.is_set():
                try:
                    streams = await bus.read_group(
                        self.stream_name,
                        self.group_name,
                        self.consumer_name,
                        count=10,
                        block_ms=2000,
                    )
                    for _, messages in streams:
                        for msg_id, data in messages:
                            payload = data
                            if isinstance(data, dict) and "data" in data:
                                try:
                                    payload = json.loads(data["data"])
                                except Exception:
                                    payload = data
                            if await self._is_duplicate(payload):
                                await bus.ack(self.stream_name, self.group_name, msg_id)
                                continue
                            await self._process_audit_event(payload)
                            await bus.ack(self.stream_name, self.group_name, msg_id)
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("reputation_engine_error error=%s", exc, exc_info=True)
                    await asyncio.sleep(5)
        finally:
            await self.stop()

    async def _is_duplicate(self, data: dict[str, Any]) -> bool:
        trace_id = str(data.get("trace_id") or "").strip()
        if not trace_id:
            return False

        redis_client = get_async_redis()
        if not redis_client:
            return False

        try:
            created = await redis_client.set(
                f"sinc:reputation:seen:{trace_id}",
                "1",
                ex=86400,
                nx=True,
            )
            return created is None
        except Exception:
            return False

    async def _process_audit_event(self, data: dict[str, Any]):
        task_type = str(data.get("task_type") or "generic").strip() or "generic"
        agent_name = str(data.get("agent_name") or "").strip()
        status = str(data.get("completion_status") or data.get("status") or "").strip()
        duration_ms = int(data.get("duration_ms") or 0)
        task_id = str(data.get("task_id") or "unknown").strip() or "unknown"
        parent_task_id = data.get("parent_task_id") or (data.get("metadata") or {}).get("parent_task_id")
        task_title = str(data.get("task_title") or "").strip()
        summary = str(data.get("summary") or "").strip()

        if not agent_name or not status:
            return
        try:
            tenant_id = self._resolve_event_tenant_id(data)
        except ValueError:
            log.error(
                "reputation_event_missing_tenant task_id=%s agent=%s status=%s",
                task_id,
                agent_name,
                status,
            )
            return

        succeeded = _is_success(status)
        affinity_col = _infer_affinity_column(task_type, task_title, summary)

        await self._update_redis(tenant_id, task_type, agent_name, succeeded, duration_ms)
        await self._update_postgres(
            tenant_id,
            task_type,
            agent_name,
            succeeded,
            duration_ms,
            affinity_col=affinity_col,
        )
        self._spawn_background_task(
            self._check_reputation_drift(tenant_id, agent_name),
            name=f"reputation.drift:{tenant_id}:{agent_name}",
        )

        try:
            from services.graph_intelligence import get_graph_intelligence

            gi = get_graph_intelligence()
            gi.sync_task_outcome(
                tenant_id=tenant_id,
                agent_name=agent_name,
                task_id=task_id,
                task_type=task_type,
                status=status,
                duration_ms=duration_ms,
                files_affected=data.get("files_affected") or data.get("files_modified") or data.get("files") or [],
            )
            if parent_task_id:
                gi.sync_task_dependency(task_id, parent_task_id)
        except Exception as exc:
            log.warning("graph_sync_failed error=%s", exc)

    async def _periodic_gds_update(self, interval_seconds: int = 300):
        log.info("background_gds_updater_started interval=%ds", interval_seconds)
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval_seconds)
                return
            except asyncio.TimeoutError:
                from services.graph_intelligence import get_graph_intelligence

                gi = get_graph_intelligence()
                await gi.run_reputation_gds(iterations=3)
                log.debug("periodic_gds_update_completed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("periodic_gds_update_failed error=%s", exc)
                await asyncio.sleep(60)

    async def _update_redis(
        self,
        tenant_id: str,
        task_type: str,
        agent_name: str,
        succeeded: bool,
        duration_ms: int,
    ) -> None:
        await async_update_agent_leaderboard(tenant_id, task_type, agent_name, succeeded)
        await async_update_agent_reputation_hash(
            tenant_id,
            task_type,
            agent_name,
            succeeded,
            duration_ms=duration_ms,
            alpha=0.15,
        )

    async def _update_postgres(
        self,
        tenant_id: str,
        task_type: str,
        agent_name: str,
        succeeded: bool,
        duration_ms: int,
        *,
        affinity_col: str | None = None,
    ) -> None:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                cols = await get_table_columns_cached(cur, "agent_reputation")
                if not cols:
                    return

                rep_has_tenant = "tenant_id" in cols
                insert_cols = ["agent_name"]
                insert_vals: list[Any] = [agent_name]
                if rep_has_tenant:
                    insert_cols.append("tenant_id")
                    insert_vals.append(tenant_id)
                if "updated_at" in cols:
                    insert_cols.append("updated_at")
                    insert_vals.append("NOW()")

                placeholders: list[str] = []
                params: list[Any] = []
                for value in insert_vals:
                    if value == "NOW()":
                        placeholders.append("NOW()")
                    else:
                        placeholders.append("%s")
                        params.append(value)

                conflict_target = "(agent_name, tenant_id)" if rep_has_tenant else "(agent_name)"
                await cur.execute(
                    f"""
                    INSERT INTO agent_reputation ({", ".join(insert_cols)})
                    VALUES ({", ".join(placeholders)})
                    ON CONFLICT {conflict_target} DO NOTHING
                    """,
                    tuple(params),
                )

                assignments: list[str] = []
                update_params: list[Any] = []
                success_increment = 1 if succeeded else 0
                failure_increment = 0 if succeeded else 1

                if "tasks_total" in cols:
                    assignments.append("tasks_total = COALESCE(tasks_total, 0) + 1")
                if "tasks_success" in cols:
                    assignments.append("tasks_success = COALESCE(tasks_success, 0) + %s")
                    update_params.append(success_increment)
                if "tasks_failure" in cols:
                    assignments.append("tasks_failure = COALESCE(tasks_failure, 0) + %s")
                    update_params.append(failure_increment)
                if "total_tasks" in cols:
                    assignments.append("total_tasks = COALESCE(total_tasks, 0) + 1")
                if "runtime_samples" in cols:
                    assignments.append("runtime_samples = COALESCE(runtime_samples, 0) + 1")

                if "runtime_success_rate" in cols:
                    if "tasks_total" in cols and "tasks_success" in cols:
                        assignments.append(
                            """
                            runtime_success_rate = ((COALESCE(tasks_success, 0) + %s)::float)
                                / GREATEST(COALESCE(tasks_total, 0) + 1, 1)
                            """
                        )
                        update_params.append(success_increment)
                    else:
                        assignments.append("runtime_success_rate = %s")
                        update_params.append(1.0 if succeeded else 0.0)

                if "success_rate" in cols:
                    if "tasks_success" in cols and "total_tasks" in cols:
                        assignments.append(
                            "success_rate = ((COALESCE(tasks_success, 0) + %s)::float) / GREATEST(COALESCE(total_tasks, 0) + 1, 1)"
                        )
                        update_params.append(success_increment)
                    elif "total_tasks" in cols:
                        assignments.append(
                            "success_rate = ((COALESCE(success_rate, 0.5) * COALESCE(total_tasks, 0)) + %s) / GREATEST(COALESCE(total_tasks, 0) + 1, 1)"
                        )
                        update_params.append(1.0 if succeeded else 0.0)
                    else:
                        assignments.append("success_rate = (COALESCE(success_rate, 0.5) * 0.9) + (%s * 0.1)")
                        update_params.append(1.0 if succeeded else 0.0)

                if "runtime_avg_duration_ms" in cols and "runtime_samples" in cols:
                    assignments.append(
                        """
                        runtime_avg_duration_ms = CASE
                            WHEN COALESCE(runtime_samples, 0) <= 0 THEN %s
                            ELSE ROUND(
                                ((COALESCE(runtime_avg_duration_ms, 0) * COALESCE(runtime_samples, 0)) + %s)
                                / (COALESCE(runtime_samples, 0) + 1.0)
                            )::int
                        END
                        """
                    )
                    update_params.extend([duration_ms, duration_ms])
                elif "runtime_avg_duration_ms" in cols:
                    assignments.append("runtime_avg_duration_ms = %s")
                    update_params.append(duration_ms)

                realtime_score = await async_get_agent_reputation_score(agent_name, tenant_id, default=0.5)
                if "reputation_fit_score" in cols:
                    assignments.append("reputation_fit_score = LEAST(1.0, GREATEST(0.0, %s))")
                    update_params.append(realtime_score)
                if "semantic_score" in cols:
                    assignments.append("semantic_score = LEAST(1.0, GREATEST(0.0, %s))")
                    update_params.append(realtime_score)
                if "is_statistically_valid" in cols:
                    assignments.append(
                        "is_statistically_valid = COALESCE(runtime_samples, COALESCE(tasks_total, 0), 0) + 1 >= 5"
                    )
                if "confidence_level" in cols:
                    assignments.append(
                        """
                        confidence_level = CASE
                            WHEN COALESCE(runtime_samples, COALESCE(tasks_total, 0), 0) + 1 >= 10 THEN 'high'
                            WHEN COALESCE(runtime_samples, COALESCE(tasks_total, 0), 0) + 1 >= 5 THEN 'medium'
                            ELSE 'low'
                        END
                        """
                    )
                if "confidence_lower" in cols:
                    assignments.append("confidence_lower = GREATEST(0.0, COALESCE(runtime_success_rate, success_rate, 0.5) - 0.15)")
                if "confidence_upper" in cols:
                    assignments.append("confidence_upper = LEAST(1.0, COALESCE(runtime_success_rate, success_rate, 0.5) + 0.15)")
                if affinity_col and affinity_col in cols:
                    assignments.append(f"{affinity_col} = LEAST(COALESCE({affinity_col}, 0.5) + 0.05, 1.0)")
                if "updated_at" in cols:
                    assignments.append("updated_at = NOW()")

                if not assignments:
                    await conn.commit()
                    return

                scope_sql = " AND tenant_id = %s" if rep_has_tenant else ""
                tail_params = [agent_name, tenant_id] if rep_has_tenant else [agent_name]
                await cur.execute(
                    f"""
                    UPDATE agent_reputation
                    SET {", ".join(" ".join(part.split()) for part in assignments)}
                    WHERE agent_name = %s{scope_sql}
                    """,
                    tuple(update_params + tail_params),
                )
                await conn.commit()

    async def _check_reputation_drift(self, tenant_id: str, agent_name: str):
        try:
            pg_rate = 0.5
            async with async_db(tenant_id=tenant_id) as conn:
                async with conn.cursor() as cur:
                    cols = await get_table_columns_cached(cur, "agent_reputation")
                    if not cols:
                        return
                    rep_has_tenant = "tenant_id" in cols
                    score_exprs = [
                        name for name in ("runtime_success_rate", "success_rate", "reputation_fit_score", "semantic_score")
                        if name in cols
                    ]
                    expr = f"COALESCE({', '.join(score_exprs)}, 0.5)" if score_exprs else "0.5"
                    await cur.execute(
                        f"""
                        SELECT {expr} AS score
                          FROM agent_reputation
                         WHERE agent_name = %s
                           {"AND tenant_id = %s" if rep_has_tenant else ""}
                        """,
                        (agent_name, tenant_id) if rep_has_tenant else (agent_name,),
                    )
                    row = await cur.fetchone()
                    if row:
                        if isinstance(row, dict):
                            pg_rate = float(row.get("score") or 0.5)
                        else:
                            pg_rate = float(row[0] or 0.5)

            redis_score = await async_get_agent_reputation_score(agent_name, tenant_id, default=0.5)
            drift = abs(pg_rate - redis_score)
            if drift > 0.3:
                log.warning(
                    "reputation_drift_detected agent=%s tenant=%s drift=%.2f pg=%.2f redis=%.2f",
                    agent_name,
                    tenant_id,
                    drift,
                    pg_rate,
                    redis_score,
                )
        except Exception as exc:
            log.debug("drift_check_failed error=%s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(ReputationEngine().start())
