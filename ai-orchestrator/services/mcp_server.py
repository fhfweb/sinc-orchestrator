from mcp.server.fastmcp import FastMCP
from services.ast_analyzer import ASTAnalyzer
from services.impact_analyzer import ImpactAnalyzer
from services.flow_mapper import FlowMapper
import os

# Initialize FastMCP server
mcp = FastMCP("SINC Cognitive Server")

# Helper to get Neo4j driver (using shared config if possible)
def get_analyzer():
    return ASTAnalyzer()

@mcp.tool()
async def query_graph(query: str, project_id: str = "default", tenant_id: str = "local") -> str:
    """
    Search the code knowledge graph for symbols, files, or relationships.
    Best for: 'Where is X defined?', 'Who calls Y?', 'Find classes extending Z'.
    """
    with get_analyzer() as analyzer:
        driver = analyzer._get_driver()
        if not driver:
            return "Neo4j driver not available."
        
        with driver.session() as session:
            # Simple keyword search fallback if not valid Cypher
            if "MATCH" not in query.upper():
                result = session.run("""
                    MATCH (n {project_id: $pid, tenant_id: $tid})
                    WHERE n.name CONTAINS $q OR n.path CONTAINS $q
                    RETURN n.name as name, n.file as file, labels(n) as type
                    LIMIT 10
                """, q=query, pid=project_id, tid=tenant_id)
            else:
                result = session.run(query, pid=project_id, tid=tenant_id)
            
            records = [dict(r) for r in result]
            if not records:
                return "No results found."
            return str(records)

@mcp.tool()
async def impact_analysis(symbol_name: str, project_id: str = "default", tenant_id: str = "local") -> str:
    """
    Calculate the blast radius of changing a specific code symbol.
    Identify callers and dependencies affected by a potential change.
    """
    with get_analyzer() as analyzer:
        driver = analyzer._get_driver()
        if not driver:
            return "Neo4j driver not available."
        
        impact_svc = ImpactAnalyzer(driver)
        result = impact_svc.analyze_impact(symbol_name, project_id, tenant_id)
        
        if not result["impact_map"]:
            return f"No impact detected for {symbol_name}."
        
        output = [f"Impact Analysis for '{symbol_name}':"]
        for imp in result["impact_map"]:
            output.append(f"  - [{imp['risk']}] {imp['type']}: {imp['name']} ({imp['file']}) - Depth: {imp['depth']}")
        
        return "\n".join(output)

@mcp.tool()
async def get_execution_flows(project_id: str = "default", tenant_id: str = "local") -> str:
    """
    List all higher-level execution flows (Processes) identified in the codebase.
    Shows the 'story' of the code (e.g., API flows, background tasks).
    """
    with get_analyzer() as analyzer:
        driver = analyzer._get_driver()
        if not driver:
            return "Neo4j driver not available."
        
        mapper = FlowMapper(driver)
        flows = mapper.get_process_stats(project_id, tenant_id)
        
        if not flows:
            return "No processes identified yet. Run a full analysis first."
        
        output = ["Identified Execution Flows:"]
        for f in flows:
            output.append(f"  - {f['name']}: {f['node_count']} steps/nodes")
        
        return "\n".join(output)

if __name__ == "__main__":
    # MCP servers usually run via stdio. FastMCP handles this automatically.
    mcp.run()
