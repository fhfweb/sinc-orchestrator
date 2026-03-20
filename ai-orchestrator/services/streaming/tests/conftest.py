from __future__ import annotations

import importlib
import os
import sys
import tempfile
import builtins
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import _pytest.pathlib as pytest_pathlib
import pytest


ROOT = Path(__file__).resolve().parents[4]
AI_ORCHESTRATOR_ROOT = ROOT / "ai-orchestrator"
SERVICES_ROOT = AI_ORCHESTRATOR_ROOT / "services"
TEMP_ROOT = ROOT / "workspace" / "tmp" / "pytest-runtime"


def _ensure_paths() -> None:
    for entry in (str(AI_ORCHESTRATOR_ROOT), str(SERVICES_ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _ensure_temp_root() -> None:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    for var_name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[var_name] = str(TEMP_ROOT)
    tempfile.tempdir = str(TEMP_ROOT)


def _install_module_aliases() -> None:
    aliases = {
        "agent_worker": "services.agent_worker",
        "cognitive_graph": "services.cognitive_graph",
        "cognitive_orchestrator": "services.cognitive_orchestrator",
        "streaming": "services.streaming",
        "streaming.core": "services.streaming.core",
        "streaming.core.auth": "services.streaming.core.auth",
        "streaming.core.db": "services.streaming.core.db",
        "streaming.core.redis_": "services.streaming.core.redis_",
        "streaming.core.sse": "services.streaming.core.sse",
        "streaming.routes": "services.streaming.routes",
        "streaming.routes.agents": "services.streaming.routes.agents",
        "streaming.routes.events": "services.streaming.routes.events",
        "streaming.routes.tasks": "services.streaming.routes.tasks",
    }
    for alias, target in aliases.items():
        try:
            module = importlib.import_module(target)
            sys.modules[alias] = module
            builtins.__dict__.setdefault(alias.split(".")[-1], module)
        except Exception:
            continue


_ORIGINAL_CLEANUP = pytest_pathlib.cleanup_dead_symlinks


def _safe_cleanup_dead_symlinks(root: Path) -> None:
    try:
        _ORIGINAL_CLEANUP(root)
    except PermissionError:
        return


pytest_pathlib.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks
_ORIGINAL_SQLITE_CONNECT = sqlite3.connect


def _patched_sqlite_connect(*args, **kwargs):
    kwargs.setdefault("check_same_thread", False)
    return _ORIGINAL_SQLITE_CONNECT(*args, **kwargs)


sqlite3.connect = _patched_sqlite_connect


def pytest_configure(config) -> None:
    _ensure_paths()
    _ensure_temp_root()
    _install_module_aliases()


def pytest_runtest_setup(item) -> None:
    _ensure_paths()
    _ensure_temp_root()
    _install_module_aliases()


@pytest.fixture
def tmp_path():
    path = TEMP_ROOT / f"case-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
