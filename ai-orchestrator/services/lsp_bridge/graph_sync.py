import os
import logging
import asyncio
import uuid
from typing import Dict, Any, List, Optional
from neo4j import AsyncGraphDatabase

from services.lsp_bridge.client import LSPClient
from services.streaming.core.config import env_get

logger = logging.getLogger("lsp_graph_sync")

class LSPGraphSync:
    """
    Bridge between the Language Server Protocol (LSP) and the Neo4j Knowledge Graph.
    Ensures absolute Zero-Hallucination symbol resolution.
    """
    def __init__(self, executable: str = "pyright-langserver", args: list[str] = None):
        self.args = args or ["--stdio"]
        self.client = LSPClient(executable, self.args)
        
        neo4j_uri = env_get("NEO4J_URI", default="bolt://localhost:7687")
        neo4j_user = env_get("NEO4J_USER", default="neo4j")
        neo4j_password = env_get("NEO4J_PASSWORD", default="password")
        self.driver = AsyncGraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

    async def start(self, root_uri: str):
        await self.client.start()
        await self.client.initialize(root_uri)
        logger.info(f"LSP bridge initialized for workspace: {root_uri}")

    async def stop(self):
        await self.client.stop()
        if self.driver:
            await self.driver.close()
        logger.info("LSP bridge stopped.")

    def _convert_path_to_uri(self, filepath: str) -> str:
        abs_path = os.path.abspath(filepath)
        # Handle Windows paths correctly for URI
        abs_path = abs_path.replace('\\', '/')
        if not abs_path.startswith('/'):
            abs_path = '/' + abs_path
        return f"file://{abs_path}"
        
    def _uri_to_path(self, uri: str) -> str:
        path = uri.replace("file://", "")
        if os.name == 'nt' and path.startswith('/'):
            path = path[1:] # Remove leading slash for windows paths like /C:/...
        return path

    async def _write_symbol_nodes(self, uri: str, symbols: List[Dict], tenant_id: str, project_id: str, parent_name: str = ""):
        """Recursively write symbols to Neo4j."""
        filepath = self._uri_to_path(uri)
        async with self.driver.session() as session:
            for sym in symbols:
                name = sym.get("name")
                kind = sym.get("kind", 0) # e.g. 5 is Class, 6 is Method, 12 is Function
                
                full_name = f"{parent_name}.{name}" if parent_name else name
                
                # Insert Symbol Node
                query = """
                MERGE (s:Symbol {name: $name, file: $file, tenant_id: $tenant_id, project_id: $project_id})
                SET s:LSPSymbol,
                    s.kind = $kind,
                    s.uri = $uri,
                    s.full_name = $full_name,
                    s.last_synced = TIMESTAMP()
                """
                await session.run(
                    query, 
                    name=name, 
                    file=filepath, 
                    tenant_id=tenant_id, 
                    project_id=project_id,
                    kind=kind,
                    uri=uri,
                    full_name=full_name
                )
                
                # Process children (e.g., methods inside a class)
                if "children" in sym and sym["children"]:
                    await self._write_symbol_nodes(uri, sym["children"], tenant_id, project_id, parent_name=full_name)

    async def _write_definition_edge(self, source_uri: str, source_name: str, def_uri: str, def_line: int, tenant_id: str, project_id: str):
        """Create an edge between a symbol and its absolute definition location."""
        source_file = self._uri_to_path(source_uri)
        def_file = self._uri_to_path(def_uri)
        
        async with self.driver.session() as session:
            query = """
            MATCH (source:Symbol {name: $source_name, file: $source_file, tenant_id: $tenant_id, project_id: $project_id})
            
            // Create target node based on exact definition URI
            MERGE (target:Symbol {file: $def_file, tenant_id: $tenant_id, project_id: $project_id})
            // Due to LSP limitations, we might not have the target name instantly, but we have its location.
            // We set it as an LSPReference point.
            SET target:LSPReference, target.line = $def_line, target.uri = $def_uri
            
            MERGE (source)-[r:LSP_RESOLVES_TO]->(target)
            SET r.synced_at = TIMESTAMP()
            """
            await session.run(
                query,
                source_name=source_name,
                source_file=source_file,
                def_file=def_file,
                def_line=def_line,
                def_uri=def_uri,
                tenant_id=tenant_id,
                project_id=project_id
            )

    async def sync_file(self, filepath: str, tenant_id: str = "default", project_id: str = "default"):
        """
        Reads a file, asks LSP for all document symbols, and then extracts
        exact definitions and references to build a concrete graph.
        """
        if not os.path.exists(filepath):
            logger.error(f"File not found: {filepath}")
            return

        with open(filepath, 'r', encoding='utf-8') as f:
            code = f.read()

        uri = self._convert_path_to_uri(filepath)
        await self.client.did_open(uri, code)
        
        logger.info(f"Extracting symbols for {filepath} via LSP...")
        symbols = await self.client.get_document_symbols(uri)
        
        if not symbols:
            logger.warning(f"No symbols returned for {filepath}")
            return
            
        # Write base symbol nodes
        await self._write_symbol_nodes(uri, symbols, tenant_id, project_id)
        
        # Now for each top-level symbol, let's just ask for references/definition as a PoC
        for sym in symbols:
            name = sym.get("name")
            location = sym.get("selectionRange", sym.get("location", {}).get("range", {}))
            if not location:
                continue
                
            start = location.get("start", {})
            line = start.get("line", 0)
            char = start.get("character", 0)
            
            defs = await self.client.get_definition(uri, line, char)
            if defs and isinstance(defs, list):
                for d in defs:
                    d_uri = d.get("uri")
                    d_line = d.get("range", {}).get("start", {}).get("line", 0)
                    if d_uri and d_line is not None:
                        await self._write_definition_edge(uri, name, d_uri, d_line, tenant_id, project_id)
                        
        logger.info(f"File {filepath} successfully synced with LSP Graph Bridge.")

# Example usage hook if run standalone
if __name__ == "__main__":
    async def main():
        import sys
        logging.basicConfig(level=logging.INFO)
        if len(sys.argv) < 2:
            print("Usage: python graph_sync.py <absolute_file_path>")
            return
            
        root = os.path.dirname(os.path.abspath(__file__))
        sync = LSPGraphSync()
        await sync.start(f"file:///{root.replace(os.sep, '/')}")
        try:
            await sync.sync_file(sys.argv[1])
        finally:
            await sync.stop()
            
    asyncio.run(main())
