from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from typing import Any, Awaitable

log = logging.getLogger("orch.background_tasks")


class BackgroundTaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, set[asyncio.Task[Any]]] = defaultdict(set)

    def spawn(self, owner: str, awaitable: Awaitable[Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name)
        bucket = self._tasks[owner]
        bucket.add(task)

        def _done(done_task: asyncio.Task[Any]) -> None:
            bucket.discard(done_task)
            if not bucket:
                self._tasks.pop(owner, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                log.info("background_task_cancelled owner=%s name=%s", owner, name)
            except Exception as exc:
                log.error(
                    "background_task_failed owner=%s name=%s error=%s",
                    owner,
                    name,
                    exc,
                    exc_info=True,
                )

        task.add_done_callback(_done)
        return task

    def has_live_tasks(self, owner: str) -> bool:
        bucket = self._tasks.get(owner, set())
        live = {task for task in bucket if not task.done()}
        if live != bucket:
            if live:
                self._tasks[owner] = live
            else:
                self._tasks.pop(owner, None)
        return bool(live)

    def snapshot(self, owner: str | None = None) -> dict[str, list[str]]:
        owners = [owner] if owner else list(self._tasks.keys())
        snap: dict[str, list[str]] = {}
        for key in owners:
            bucket = [task.get_name() for task in self._tasks.get(key, set()) if not task.done()]
            if bucket:
                snap[key] = sorted(bucket)
        return snap

    async def cancel_owner(self, owner: str, *, timeout_s: float = 5.0) -> None:
        tasks = [task for task in self._tasks.get(owner, set()) if not task.done()]
        if not tasks:
            self._tasks.pop(owner, None)
            return
        for task in tasks:
            task.cancel()
        done, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in done:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
        for task in pending:
            log.warning("background_task_cancel_timeout owner=%s name=%s", owner, task.get_name())
        self._tasks.pop(owner, None)

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        for owner in list(self._tasks.keys()):
            await self.cancel_owner(owner, timeout_s=timeout)


_REGISTRY = BackgroundTaskRegistry()


def get_background_task_registry() -> BackgroundTaskRegistry:
    return _REGISTRY
