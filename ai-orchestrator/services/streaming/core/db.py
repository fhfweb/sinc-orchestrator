"""
Database connection pool, context managers, and slow-query instrumentation.

This module keeps the public API stable:
  - get_pool()
  - get_async_pool()
  - db(...)
  - async_db(...)

The async path is hardened for Windows by forcing a selector event loop policy
when possible and by avoiding deprecated eager-open pool construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from typing import Optional

import psycopg
import psycopg.rows

from .config import DB_CONFIG, DB_POOL_MAX, SLOW_QUERY_MS

log = logging.getLogger("orchestrator")

_DB_CONNINFO = (
    f"dbname={DB_CONFIG['dbname']} user={DB_CONFIG['user']} "
    f"password={DB_CONFIG['password']} host={DB_CONFIG['host']} port={DB_CONFIG['port']}"
    f" sslmode={DB_CONFIG['sslmode']} connect_timeout={DB_CONFIG['connect_timeout']}"
)

_db_pool = None
_async_db_pool = None


def _ensure_windows_asyncio_policy() -> None:
    if sys.platform != "win32":
        return
    try:
        selector_policy = asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
        current_policy = asyncio.get_event_loop_policy()
        if current_policy.__class__ is not selector_policy.__class__:
            asyncio.set_event_loop_policy(selector_policy)
            log.info("asyncio_policy_set policy=WindowsSelectorEventLoopPolicy")
    except Exception as exc:
        log.debug("asyncio_policy_set_failed error=%s", exc)


_ensure_windows_asyncio_policy()


def get_pool():
    global _db_pool
    if _db_pool is not None:
        return _db_pool
    try:
        from psycopg_pool import ConnectionPool

        _db_pool = ConnectionPool(
            conninfo=_DB_CONNINFO,
            min_size=2,
            max_size=DB_POOL_MAX,
            kwargs={"row_factory": psycopg.rows.dict_row},
        )
        log.info("db_pool_initialized min=2 max=%s", DB_POOL_MAX)
        return _db_pool
    except ImportError:
        log.warning("psycopg_pool not available - using single connections")
        return None


def get_async_pool():
    global _async_db_pool
    if _async_db_pool is not None:
        return _async_db_pool
    try:
        from psycopg_pool import AsyncConnectionPool

        _async_db_pool = AsyncConnectionPool(
            conninfo=_DB_CONNINFO,
            min_size=2,
            max_size=DB_POOL_MAX,
            open=False,
            kwargs={"row_factory": psycopg.rows.dict_row},
        )
        log.info("async_db_pool_initialized min=2 max=%s", DB_POOL_MAX)
        return _async_db_pool
    except ImportError:
        log.warning("psycopg_pool (Async) not available")
        return None


class _TimedCursor:
    """Wrap a psycopg cursor and log slow queries."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, query, params=None):
        t0 = time.time()
        result = self._cur.execute(query, params)
        elapsed_ms = (time.time() - t0) * 1000
        if elapsed_ms >= SLOW_QUERY_MS:
            q_short = str(query)[:120].replace("\n", " ")
            log.warning("slow_query ms=%.1f query=%r", elapsed_ms, q_short)
        return result

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *args):
        return self._cur.__exit__(*args)


class _TimedConn:
    """Wrap a psycopg connection and instrument its cursor."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *args, **kwargs):
        return _TimedCursor(self._conn.cursor(*args, **kwargs))

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)


@contextlib.contextmanager
def db(tenant_id: Optional[str] = None, bypass_rls: bool = False):
    """
    Sync context manager.

    `bypass_rls` is only for maintenance or global aggregation paths.
    """
    pool = get_pool()
    if pool:
        with pool.connection() as raw:
            if bypass_rls:
                raw.execute("SELECT set_config('app.bypass_rls', 'on', false)")
            if tenant_id:
                raw.execute(
                    "SELECT set_config('app.current_tenant', %s, false)",
                    (tenant_id,),
                )
            yield _TimedConn(raw)
    else:
        with psycopg.connect(_DB_CONNINFO, row_factory=psycopg.rows.dict_row) as raw:
            if bypass_rls:
                raw.execute("SELECT set_config('app.bypass_rls', 'on', false)")
            if tenant_id:
                raw.execute(
                    "SELECT set_config('app.current_tenant', %s, false)",
                    (tenant_id,),
                )
            yield _TimedConn(raw)


@contextlib.asynccontextmanager
async def async_db(tenant_id: Optional[str] = None, bypass_rls: bool = False):
    """Async context manager yielding an AsyncConnection."""
    pool = get_async_pool()
    if pool:
        if getattr(pool, "closed", True):
            await pool.open()
        async with pool.connection() as raw:
            if bypass_rls:
                await raw.execute("SELECT set_config('app.bypass_rls', 'on', false)")
            if tenant_id:
                await raw.execute(
                    "SELECT set_config('app.current_tenant', %s, false)",
                    (tenant_id,),
                )
            yield raw
    else:
        async with await psycopg.AsyncConnection.connect(
            _DB_CONNINFO,
            row_factory=psycopg.rows.dict_row,
        ) as raw:
            if bypass_rls:
                await raw.execute("SELECT set_config('app.bypass_rls', 'on', false)")
            if tenant_id:
                await raw.execute(
                    "SELECT set_config('app.current_tenant', %s, false)",
                    (tenant_id,),
                )
            yield raw
