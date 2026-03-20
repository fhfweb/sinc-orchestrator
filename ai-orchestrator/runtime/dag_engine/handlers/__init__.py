"""
Native task handler registry.
Maps task ID prefixes to handler classes.
"""

from .base_handler import BaseHandler
from .repair_handler import RepairHandler

HANDLER_REGISTRY: list[type[BaseHandler]] = [
    RepairHandler,
]


def get_handler(task: dict) -> BaseHandler | None:
    """Return the first handler that can handle the given task, or None."""
    for handler_cls in HANDLER_REGISTRY:
        handler = handler_cls(task)
        if handler.can_handle():
            return handler
    return None
