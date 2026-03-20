from __future__ import annotations

from services.background_tasks import get_background_task_registry


def get_task_registry():
    return get_background_task_registry()
