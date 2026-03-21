import pytest
from unittest.mock import AsyncMock, patch
from services.lsp_bridge.graph_sync import LSPGraphSync

@pytest.mark.asyncio
async def test_graph_sync_initialization():
    with patch('neo4j.AsyncGraphDatabase.driver') as mock_driver:
        # Provide an async close method on the mocked driver instance
        mock_driver_instance = AsyncMock()
        mock_driver.return_value = mock_driver_instance
        
        sync = LSPGraphSync()
        assert sync.client is not None
        mock_driver.assert_called_once()
        
        # Test stop
        sync.client.stop = AsyncMock()
        await sync.stop()
        
        sync.client.stop.assert_called_once()
        mock_driver_instance.close.assert_called_once()
