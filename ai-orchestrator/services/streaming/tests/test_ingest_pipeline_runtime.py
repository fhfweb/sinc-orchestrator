from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest


@pytest.mark.asyncio
async def test_ingest_stream_consumer_start_uses_event_bus_without_name_errors():
    from services.ingest_pipeline import IngestPipeline, IngestStreamConsumer

    fake_bus = Mock()
    fake_bus.consume = AsyncMock(return_value=None)

    with patch("services.event_bus.get_event_bus", AsyncMock(return_value=fake_bus)):
        consumer = IngestStreamConsumer(IngestPipeline())
        await consumer.start()

    fake_bus.consume.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_stream_consumer_process_event_runs_pipeline_in_thread():
    from services.ingest_pipeline import IngestPipeline, IngestStreamConsumer

    pipeline = IngestPipeline()
    consumer = IngestStreamConsumer(pipeline)

    with patch.object(pipeline, "run", Mock(return_value={"ok": True})) as run_mock:
        await consumer._process_event(
            {
                "pipeline_id": "ingest-123",
                "project_path": "/tmp/project",
                "project_id": "sinc",
                "tenant_id": "local",
                "deep": True,
            }
        )

    run_mock.assert_called_once_with(
        "ingest-123",
        project_path="/tmp/project",
        project_id="sinc",
        tenant_id="local",
        deep=True,
        repo_url="",
        branch="",
    )
