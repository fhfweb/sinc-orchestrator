import os
import json
import uuid
import httpx
from mcp.server.fastmcp import FastMCP
from services.ast_analyzer import ASTAnalyzer
from services.impact_analyzer import ImpactAnalyzer
from services.flow_mapper import FlowMapper
from services.streaming.core.config import env_get
from services.http_client import create_resilient_client

# Initialize FastMCP server
mcp = FastMCP("SINC Cognitive Server")

# Helper to get Neo4j driver (using shared config if possible)
def get_analyzer():
    return ASTAnalyzer()

async def _orchestrator_request(method: str, path: str, body: dict = None, tenant_id: str = "local") -> dict:
    base_url = env_get("ORCHESTRATOR_URL", default="http://localhost:8000").rstrip("/")
    api_key = env_get("ORCHESTRATOR_API_KEY", default="")
    
    headers = {
        "X-Tenant-Id": tenant_id,
        "X-Trace-Id": f"mcp-{uuid.uuid4().hex[:8]}",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with create_resilient_client(service_name="mcp-server") as client:
        try:
            response = await client.request(method, f"{base_url}{path}", json=body, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return {"error": str(e)}

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
            return json.dumps(records, indent=2)

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
async def create_sinc_task(title: str, description: str, agent: str = "ai engineer", tenant_id: str = "local") -> str:
    """
    Create a new task in the SINC Orchestrator. 
    The orchestrator will dispatch this to the appropriate worker.
    """
    payload = {
        "title": title,
        "description": description,
        "agent": agent
    }
    res = await _orchestrator_request("POST", "/api/v1/tasks", body=payload, tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def get_task_status(task_id: str, tenant_id: str = "local") -> str:
    """Check the status and result of a specific SINC task."""
    res = await _orchestrator_request("GET", f"/api/v1/tasks/{task_id}", tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def search_agent_memory(query: str, tenant_id: str = "local", top_k: int = 5) -> str:
    """
    Search the SINC semantic memory (Qdrant) for relevant past experiences, 
    code patterns, or project knowledge.
    """
    payload = {"query": query, "top_k": top_k}
    res = await _orchestrator_request("POST", "/api/v1/cognitive/memory/search", body=payload, tenant_id=tenant_id)
    return json.dumps(res, indent=2)

@mcp.tool()
async def get_orchestrator_capabilities() -> str:
    """
    Discovery tool: Returns the list of currently available agents, 
    active projects, and system health.
    """
    res = await _orchestrator_request("GET", "/api/v1/system/capabilities")
    return json.dumps(res, indent=2)

if __name__ == "__main__":
    mcp.run()
