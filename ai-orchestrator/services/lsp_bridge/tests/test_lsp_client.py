import pytest
from unittest.mock import AsyncMock, patch
from services.lsp_bridge.client import LSPClient

@pytest.mark.asyncio
async def test_lsp_client_lifecycle():
    with patch('asyncio.create_subprocess_exec') as mock_exec:
        mock_process = AsyncMock()
        mock_process.returncode = None
        mock_exec.return_value = mock_process
        
        client = LSPClient("dummy-langserver", ["--stdio"])
        await client.start()
        
        mock_exec.assert_called_once()
        assert client.process is not None
        
        await client.stop()
        mock_process.terminate.assert_called_once()
