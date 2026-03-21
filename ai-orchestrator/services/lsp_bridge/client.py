import asyncio
import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("lsp_client")

class LSPClient:
    """
    A minimal JSON-RPC client to communicate with Language Servers via stdio.
    """
    def __init__(self, executable: str, args: list[str]):
        self.executable = executable
        self.args = args
        self.process: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None

    async def start(self):
        """Spawns the language server process and starts the read loop."""
        logger.info(f"Starting LSP server: {self.executable} {self.args}")
        self.process = await asyncio.create_subprocess_exec(
            self.executable, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        self._read_task = asyncio.create_task(self._read_loop())

    async def stop(self):
        """Terminates the language server."""
        if self.process:
            try:
                self.process.terminate()
                await self.process.wait()
            except Exception as e:
                logger.warning(f"Error terminating LSP server: {e}")
        if self._read_task:
            self._read_task.cancel()

    async def _read_loop(self):
        """Continuously reads JSON-RPC messages from stdout."""
        while self.process and self.process.returncode is None:
            try:
                line = await self.process.stdout.readline()
                if not line:
                    break
                line = line.decode('utf-8').strip()
                if not line.startswith("Content-Length:"):
                    continue
                
                length = int(line.split(":")[1].strip())
                # Skip empty line
                await self.process.stdout.readline()
                
                content = await self.process.stdout.readexactly(length)
                message = json.loads(content.decode('utf-8'))
                
                if "id" in message and message["id"] in self._pending_requests:
                    future = self._pending_requests.pop(message["id"])
                    if not future.done():
                        if "error" in message:
                            future.set_exception(Exception(message["error"]))
                        else:
                            future.set_result(message.get("result"))
                else:
                    # Notifications or unsolicited requests from server
                    method = message.get("method")
                    logger.debug(f"Received Server Notification/Request: {method}")
                    
            except asyncio.IncompleteReadError:
                break
            except Exception as e:
                logger.error(f"LSP Read Loop Error: {e}")
                break

    def _create_message(self, method: str, params: Optional[Dict] = None, is_request: bool = True) -> Tuple[bytes, Optional[int]]:
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method
        }
        req_id = None
        if is_request:
            self._request_id += 1
            req_id = self._request_id
            msg["id"] = req_id
            
        if params is not None:
            msg["params"] = params
            
        body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        return header + body, req_id

    async def _send(self, method: str, params: Optional[Dict], is_request: bool = True) -> Optional[Any]:
        if not self.process or not self.process.stdin:
            raise RuntimeError("LSP client not started or stdin not available")

        message_bytes, req_id = self._create_message(method, params, is_request)
        
        future = None
        if is_request and req_id is not None:
            future = asyncio.get_event_loop().create_future()
            self._pending_requests[req_id] = future
            
        self.process.stdin.write(message_bytes)
        await self.process.stdin.drain()
        
        if is_request and future:
            return await future
        return None

    async def initialize(self, root_uri: str) -> dict:
        """Sends the JSON-RPC initialize request."""
        params = {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": True},
                    "references": {"dynamicRegistration": True}
                }
            }
        }
        res = await self._send("initialize", params)
        await self._send("initialized", {}, is_request=False)
        return res

    async def did_open(self, uri: str, text: str, language_id: str = "python"):
        """Sends textDocument/didOpen notification."""
        params = {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text
            }
        }
        await self._send("textDocument/didOpen", params, is_request=False)

    async def get_definition(self, uri: str, line: int, character: int) -> list:
        """Sends textDocument/definition request."""
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character}
        }
        res = await self._send("textDocument/definition", params)
        return res if res else []

    async def get_references(self, uri: str, line: int, character: int) -> list:
        """Sends textDocument/references request."""
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": True}
        }
        res = await self._send("textDocument/references", params)
        return res if res else []

    async def get_document_symbols(self, uri: str) -> list:
        """Sends textDocument/documentSymbol request to get all symbols in file."""
        params = {
            "textDocument": {"uri": uri}
        }
        res = await self._send("textDocument/documentSymbol", params)
        return res if res else []
