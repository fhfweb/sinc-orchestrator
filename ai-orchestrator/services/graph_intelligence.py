import asyncio
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from neo4j import Driver, GraphDatabase

from services.streaming.core.redis_ import get_async_redis

log = logging.getLogger("orch.graph_intelligence")


class GraphIntelligenceService:
    def __init__(self, uri: str = None, user: str = None, password: str = None):
        try:
            from services.streaming.core.config import env_get, _running_in_container
        except ImportError:
            env_get = lambda *n, default=None: os.environ.get(n[0], default)
            _running_in_container = lambda: os.path.exists("/.dockerenv")

        self._uri = uri or env_get("NEO4J_URI", default="bolt://localhost:7687")
        if not _running_in_container() and "bolt://neo4j" in self._uri:
            self._uri = self._uri.replace("bolt://neo4j", "bolt://localhost")

        auth_env = env_get("NEO4J_AUTH", default="neo4j/neo4j")
        default_user = auth_env.split("/", 1)[0] if "/" in auth_env else "neo4j"
        default_pass = auth_env.split("/", 1)[-1] if "/" in auth_env else "neo4j"

        self._user = user or env_get("NEO4J_USER", default=default_user)
        self._password = password or env_get("NEO4J_PASS", default=default_pass)
        self._driver: Optional[Driver] = None
        self._lock = asyncio.Lock()
        self._projection_name = "reputationGraph_v2"
        self._gds_min_run_interval_s = float(env_get("GDS_MIN_RUN_INTERVAL_SECONDS", default="60"))
        self._gds_projection_refresh_interval_s = float(
            env_get("GDS_PROJECTION_REFRESH_INTERVAL_SECONDS", default="900")
        )
        self._gds_lease_ttl_s = int(float(env_get("GDS_LEASE_TTL_SECONDS", default="180")))
        self._gds_lease_renew_interval_s = max(
            5.0,
            float(env_get("GDS_LEASE_RENEW_INTERVAL_SECONDS", default=str(max(self._gds_lease_ttl_s / 3, 5)))),
        )
        self._gds_last_run_at = 0.0
        self._gds_last_projection_refresh_at = 0.0
        self._has_gds = None
        self._lease_key = f"sinc:gds:lease:{self._projection_name}"
        self._lease_fence_key = f"sinc:gds:fence:{self._projection_name}"
        self._last_run_key = f"sinc:gds:last_run:{self._projection_name}"
        self._last_refresh_key = f"sinc:gds:last_refresh:{self._projection_name}"

    def _get_driver(self) -> Driver:
        if not self._driver:
            self._driver = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
        return self._driver

    async def _check_gds(self) -> bool:
        if self._has_gds is not None:
            return self._has_gds
        try:
            def _probe():
                with self._get_driver().session() as session:
                    return session.run("RETURN gds.version() AS v").single() is not None

            self._has_gds = await asyncio.to_thread(_probe)
        except Exception:
            log.warning("gds_plugin_not_found - analytics will be limited")
            self._has_gds = False
        return self._has_gds

    def sync_task_outcome(
        self,
        tenant_id: str,
        agent_name: str,
        task_id: str,
        task_type: str,
        status: str,
        duration_ms: int = 0,
        files_affected: List[str] = None,
    ):
        files = files_affected or []
        success = str(status or "").lower() in ("done", "success", "completed")
        query = """
        MERGE (a:Agent {name: $agent_name, tenant_id: $tenant_id})
        MERGE (t:Task {task_id: $task_id, tenant_id: $tenant_id})
        SET t.task_type = $task_type,
            t.status = $status,
            t.success = $success,
            t.duration_ms = $duration_ms,
            t.updated_at = datetime()
        MERGE (a)-[r:PERFORMED]->(t)
        SET r.updated_at = datetime()
        WITH t
        UNWIND $files AS f
        MERGE (file:File {path: f, tenant_id: $tenant_id})
        MERGE (t)-[:AFFECTED]->(file)
        """
        try:
            with self._get_driver().session() as session:
                session.run(
                    query,
                    agent_name=agent_name,
                    tenant_id=tenant_id,
                    task_id=task_id,
                    task_type=task_type,
                    status=status,
                    success=success,
                    duration_ms=duration_ms,
                    files=files,
                )
        except Exception as exc:
            log.debug("sync_task_outcome_failed error=%s", exc)

    def sync_task_dependency(self, task_id: str, parent_task_id: str):
        query = """
        MERGE (child:Task {task_id: $task_id})
        MERGE (parent:Task {task_id: $parent_task_id})
        MERGE (child)-[:DEPENDS_ON]->(parent)
        """
        try:
            with self._get_driver().session() as session:
                session.run(query, task_id=task_id, parent_task_id=parent_task_id)
        except Exception as exc:
            log.debug("sync_task_dependency_failed error=%s", exc)

    async def _acquire_distributed_lease(self) -> tuple[str | None, str]:
        redis_client = get_async_redis()
        if not redis_client:
            return None, "local-only"
        token = uuid.uuid4().hex
        ttl = max(self._gds_lease_ttl_s, int(self._gds_min_run_interval_s) + 30)
        try:
            if hasattr(redis_client, "eval"):
                result = await redis_client.eval(
                    """
                    if redis.call('EXISTS', KEYS[1]) == 0 then
                        local fence = redis.call('INCR', KEYS[2])
                        local lease_value = ARGV[1] .. ':' .. tostring(fence)
                        redis.call('SET', KEYS[1], lease_value, 'EX', ARGV[2])
                        return {1, tostring(fence), lease_value}
                    end
                    return {0, '0', ''}
                    """,
                    2,
                    self._lease_key,
                    self._lease_fence_key,
                    token,
                    str(ttl),
                )
                if isinstance(result, (list, tuple)) and result:
                    acquired = bool(int(result[0]))
                    if acquired:
                        lease_value = str(result[2] if len(result) > 2 else token)
                        return lease_value, "distributed"
                    return "", "distributed"
            acquired = await redis_client.set(self._lease_key, token, ex=ttl, nx=True)
            if acquired:
                fence_token = None
                if hasattr(redis_client, "incr"):
                    try:
                        fence_token = await redis_client.incr(self._lease_fence_key)
                    except Exception:
                        fence_token = None
                lease_value = f"{token}:{fence_token}" if fence_token is not None else token
                if lease_value != token and hasattr(redis_client, "set"):
                    try:
                        await redis_client.set(self._lease_key, lease_value, ex=ttl)
                    except Exception:
                        lease_value = token
                return lease_value, "distributed"
            return "", "distributed"
        except Exception as exc:
            log.warning("gds_lease_acquire_failed error=%s", exc)
            return None, "local-only"

    async def _release_distributed_lease(self, token: str | None) -> None:
        if not token:
            return
        redis_client = get_async_redis()
        if not redis_client:
            return
        try:
            await redis_client.eval(
                """
                if redis.call('GET', KEYS[1]) == ARGV[1] then
                    return redis.call('DEL', KEYS[1])
                end
                return 0
                """,
                1,
                self._lease_key,
                token,
            )
        except Exception as exc:
            log.debug("gds_lease_release_failed error=%s", exc)

    async def _renew_distributed_lease(self, token: str | None) -> bool:
        if not token:
            return False
        redis_client = get_async_redis()
        if not redis_client:
            return False
        try:
            renewed = await redis_client.eval(
                """
                if redis.call('GET', KEYS[1]) == ARGV[1] then
                    return redis.call('EXPIRE', KEYS[1], ARGV[2])
                end
                return 0
                """,
                1,
                self._lease_key,
                token,
                str(max(self._gds_lease_ttl_s, int(self._gds_min_run_interval_s) + 30)),
            )
            return bool(renewed)
        except Exception as exc:
            log.debug("gds_lease_renew_failed error=%s", exc)
            return False

    async def _lease_heartbeat_loop(self, token: str) -> None:
        while True:
            await asyncio.sleep(self._gds_lease_renew_interval_s)
            renewed = await self._renew_distributed_lease(token)
            if not renewed:
                log.warning("gds_lease_renew_lost projection=%s", self._projection_name)
                return

    async def _read_cluster_timestamps(self) -> tuple[float, float]:
        redis_client = get_async_redis()
        if not redis_client:
            return 0.0, 0.0
        try:
            last_run_raw, last_refresh_raw = await redis_client.mget(self._last_run_key, self._last_refresh_key)
            return float(last_run_raw or 0.0), float(last_refresh_raw or 0.0)
        except Exception as exc:
            log.debug("gds_cluster_timestamp_read_failed error=%s", exc)
            return 0.0, 0.0

    async def _write_cluster_timestamps(self, *, last_run_at: float, last_refresh_at: float | None = None) -> None:
        redis_client = get_async_redis()
        if not redis_client:
            return
        try:
            mapping = {self._last_run_key: str(last_run_at)}
            if last_refresh_at is not None:
                mapping[self._last_refresh_key] = str(last_refresh_at)
            pipe = redis_client.pipeline()
            ttl = int(max(self._gds_projection_refresh_interval_s * 2, 3600))
            for key, value in mapping.items():
                await pipe.setex(key, ttl, value)
            await pipe.execute()
        except Exception as exc:
            log.debug("gds_cluster_timestamp_write_failed error=%s", exc)

    async def run_reputation_gds(
        self,
        tenant_id: str | None = None,
        *,
        force: bool = False,
        iterations: int = 20,
    ) -> Dict[str, Any]:
        if not await self._check_gds():
            return {"status": "skipped", "reason": "gds_not_installed"}

        async with self._lock:
            now = time.time()
            if not force and self._gds_last_run_at and now - self._gds_last_run_at < self._gds_min_run_interval_s:
                return {"status": "skipped", "reason": "rate_limited", "lease_mode": "process"}

            lease_token, lease_mode = await self._acquire_distributed_lease()
            if lease_mode == "distributed" and lease_token == "":
                return {"status": "skipped", "reason": "lease_held", "lease_mode": "distributed"}
            lease_heartbeat_task: asyncio.Task | None = None
            fence_token = ""
            if lease_token and ":" in lease_token:
                fence_token = lease_token.rsplit(":", 1)[-1]

            cluster_last_run, cluster_last_refresh = await self._read_cluster_timestamps()
            if not force and cluster_last_run and now - cluster_last_run < self._gds_min_run_interval_s:
                await self._release_distributed_lease(lease_token)
                self._gds_last_run_at = cluster_last_run
                return {"status": "skipped", "reason": "cluster_rate_limited", "lease_mode": lease_mode}

            try:
                if lease_mode == "distributed" and lease_token:
                    lease_heartbeat_task = asyncio.create_task(
                        self._lease_heartbeat_loop(lease_token),
                        name=f"gds.lease_heartbeat:{self._projection_name}",
                    )
                projection_refresh_at = max(self._gds_last_projection_refresh_at, cluster_last_refresh)
                projection_stale = not projection_refresh_at or (
                    now - projection_refresh_at > self._gds_projection_refresh_interval_s
                )

                def _run() -> dict[str, Any]:
                    with self._get_driver().session() as session:
                        exists_row = session.run(
                            "CALL gds.graph.exists($name) YIELD exists",
                            name=self._projection_name,
                        ).single()
                        exists = bool(exists_row["exists"]) if exists_row else False

                        rebuilt = False
                        if projection_stale and exists:
                            session.run("CALL gds.graph.drop($name, false)", name=self._projection_name)
                            exists = False
                            rebuilt = True

                        if not exists:
                            session.run(
                                """
                                CALL gds.graph.project(
                                    $name,
                                    {
                                        Agent: {label: 'Agent'},
                                        Task: {label: 'Task', properties: ['success']}
                                    },
                                    {
                                        PERFORMED: {type: 'PERFORMED', orientation: 'NATURAL'},
                                        DEPENDS_ON: {type: 'DEPENDS_ON', orientation: 'REVERSE'}
                                    }
                                )
                                """,
                                name=self._projection_name,
                            )
                            rebuilt = True

                        session.run(
                            """
                            CALL gds.pageRank.write(
                                $name,
                                {
                                    writeProperty: 'pagerank_score',
                                    maxIterations: $iterations,
                                    dampingFactor: 0.85,
                                    scaler: 'MIN_MAX'
                                }
                            )
                            """,
                            name=self._projection_name,
                            iterations=iterations,
                        )
                        session.run(
                            "CALL gds.degree.write($name, {writeProperty: 'centrality_score'})",
                            name=self._projection_name,
                        )
                        return {"status": "ok", "projection_rebuilt": rebuilt}

                result = await asyncio.to_thread(_run)
                self._gds_last_run_at = now
                refresh_at = now if result.get("projection_rebuilt") else None
                if refresh_at is not None:
                    self._gds_last_projection_refresh_at = refresh_at
                await self._write_cluster_timestamps(last_run_at=now, last_refresh_at=refresh_at)
                log.info(
                    "gds_lifecycle_complete tenant=%s rebuilt=%s lease_mode=%s fence_token=%s",
                    tenant_id or "global",
                    result.get("projection_rebuilt"),
                    lease_mode,
                    fence_token or "none",
                )
                return {
                    **result,
                    "lease_mode": lease_mode,
                    "tenant_scope": tenant_id or "global",
                    "fence_token": fence_token or None,
                }
            except Exception as exc:
                log.error("run_reputation_gds_failed error=%s", exc)
                return {
                    "status": "error",
                    "error": str(exc),
                    "lease_mode": lease_mode,
                    "fence_token": fence_token or None,
                }
            finally:
                if lease_heartbeat_task:
                    lease_heartbeat_task.cancel()
                    await asyncio.gather(lease_heartbeat_task, return_exceptions=True)
                await self._release_distributed_lease(lease_token)

    def get_agent_metrics(self, agent_name: str, tenant_id: str = "local") -> Dict[str, Any]:
        query = """
        MATCH (a:Agent {name: $agent_name, tenant_id: $tenant_id})
        RETURN a.pagerank_score AS pagerank, a.centrality_score AS centrality
        """
        try:
            with self._get_driver().session() as session:
                row = session.run(query, agent_name=agent_name, tenant_id=tenant_id).single()
                if row:
                    return {
                        "pagerank": row["pagerank"] or 0.15,
                        "centrality": row["centrality"] or 0.0,
                    }
        except Exception:
            pass
        return {"pagerank": 0.15, "centrality": 0.0}

    def close(self):
        if self._driver:
            self._driver.close()


_instance: Optional[GraphIntelligenceService] = None


def get_graph_intelligence() -> GraphIntelligenceService:
    global _instance
    if _instance is None:
        _instance = GraphIntelligenceService()
    return _instance
