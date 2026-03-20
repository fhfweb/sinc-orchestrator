import json
import os
import subprocess
import shutil
from pathlib import Path


def test_memory_sync_uses_project_pack_root() -> None:
    project_slug = "pack-isolation-fixture"
    repo_root = Path(__file__).resolve().parents[2]
    pytest_tmp_root = repo_root / "workspace" / "tmp" / "pytest-fixtures"
    pytest_tmp_root.mkdir(parents=True, exist_ok=True)
    test_root = pytest_tmp_root / "pack-isolation-fixed"
    if test_root.exists():
        shutil.rmtree(test_root, ignore_errors=True)
    test_root.mkdir(parents=True, exist_ok=True)

    pack_root = test_root / "ai-orchestrator" / "projects" / project_slug
    memory_dir = pack_root / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "README.md").write_text("# Scoped Memory\n", encoding="utf-8")

    script_path = repo_root / "scripts" / "memory_sync.py"
    env = os.environ.copy()
    env["PROJECT_PACK_ROOT"] = str(pack_root)

    completed = subprocess.run(
        ["python", str(script_path), "--project-slug", project_slug, "--skip-qdrant", "--skip-neo4j"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = json.loads(completed.stdout)
    assert payload["memory_dir"].lower() == str(memory_dir.resolve()).lower()
    assert payload["relationships_path"].lower().endswith(
        str(Path("memory") / "relationships.md").lower()
    )
