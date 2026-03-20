import pytest

from services import memory_compaction


def test_group_points_splits_incident_and_file_views():
    incident_groups, file_groups = memory_compaction._group_points(
        [
            {
                "id": "p1",
                "payload": {
                    "content": "validation failed in services/foo.py",
                    "metadata": {
                        "incident_family": "validation",
                        "files": ["services/foo.py"],
                    },
                },
            },
            {
                "id": "p2",
                "payload": {
                    "content": "same file failed again",
                    "metadata": {
                        "incident_family": "validation",
                        "files": ["services/foo.py"],
                    },
                },
            },
        ]
    )

    assert len(incident_groups["validation"]) == 2
    assert len(file_groups["services/foo.py"]) == 2


def test_hint_match_score_prefers_project_file_and_incident_matches():
    score = memory_compaction._hint_match_score(
        {
            "project_id": "sinc",
            "task_type": "fix_bug",
            "incident_family": "validation",
            "file_path": "services/foo.py",
            "strength": 0.9,
            "metadata": {"files": ["services/foo.py"], "source_ids": ["a", "b"]},
        },
        project_id="sinc",
        task_type="fix_bug",
        file_path="services/foo.py",
        incident_family="validation",
    )
    weaker = memory_compaction._hint_match_score(
        {
            "project_id": "",
            "task_type": "",
            "incident_family": "",
            "file_path": "",
            "strength": 0.2,
            "metadata": {},
        },
        project_id="sinc",
        task_type="fix_bug",
        file_path="services/foo.py",
        incident_family="validation",
    )

    assert score > weaker


@pytest.mark.asyncio
async def test_compact_memory_once_generates_reactivation_hints(monkeypatch):
    upserts = []
    hints = []
    runs = []

    async def fake_schema():
        return None

    async def fake_upsert_hint(**kwargs):
        hints.append(kwargs)

    async def fake_record_run(**kwargs):
        runs.append(kwargs)

    monkeypatch.setattr(memory_compaction, "ensure_memory_compaction_schema", fake_schema)
    monkeypatch.setattr(memory_compaction, "_list_memory_collections", lambda: ["local_sinc_agent_memory"])
    monkeypatch.setattr(
        memory_compaction,
        "_scroll_collection",
        lambda _collection, _limit=0: [
            {
                "id": "p1",
                "payload": {
                    "tenant_id": "local",
                    "project_id": "sinc",
                    "content": "Validation failed in services/foo.py due to stale schema.",
                    "timestamp": "2026-03-19T10:00:00+00:00",
                    "metadata": {
                        "incident_family": "validation",
                        "files": ["services/foo.py"],
                        "task_type": "fix_bug",
                        "status": "failed",
                    },
                },
            },
            {
                "id": "p2",
                "payload": {
                    "tenant_id": "local",
                    "project_id": "sinc",
                    "content": "Validated fix for services/foo.py requires rerunning migrations.",
                    "timestamp": "2026-03-19T11:00:00+00:00",
                    "metadata": {
                        "incident_family": "validation",
                        "files": ["services/foo.py"],
                        "task_type": "fix_bug",
                        "status": "done",
                    },
                },
            },
        ],
    )
    monkeypatch.setattr(memory_compaction, "_embed_text", lambda _text: ([0.1, 0.2, 0.3], None))
    monkeypatch.setattr(memory_compaction, "_upsert_qdrant", lambda collection, vector, payload: upserts.append((collection, payload)) or None)
    monkeypatch.setattr(memory_compaction, "_upsert_reactivation_hint", fake_upsert_hint)
    monkeypatch.setattr(memory_compaction, "_record_compaction_run", fake_record_run)

    summary = await memory_compaction.compact_memory_once()

    assert summary["status"] == "ok"
    assert summary["compacted"] >= 1
    assert upserts
    assert hints
    assert runs
