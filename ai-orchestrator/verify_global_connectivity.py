import os
import time
import asyncio
from services.ast_analyzer import ASTAnalyzer
from services.xref_resolver import XRefResolver
from neo4j import GraphDatabase
from services.streaming.core.config import env_get

async def test_global_connectivity():
    print("--- 🌍 Testing Global Connectivity: Phase 16 ---")
    root_path = "tmp_global"
    os.makedirs(f"{root_path}/auth_service", exist_ok=True)
    os.makedirs(f"{root_path}/client_app", exist_ok=True)
    tenant_id = f"global_test_{int(time.time())}"

    # 1. Create Python Service (API Endpoint)
    with open(f"{root_path}/auth_service/api.py", "w") as f:
        f.write('''
@app.get("/api/v1/login")
def login_endpoint():
    return {"status": "ok"}
''')

    # 2. Create JS Client (Network Call)
    with open(f"{root_path}/client_app/client.js", "w") as f:
        f.write('''
async function performLogin() {
    const res = await axios.get("/api/v1/login");
    console.log(res);
}
''')

    # 3. Run Global Analysis
    print("Step 1: Analyzing Global Projects...")
    with ASTAnalyzer() as analyzer:
        analyzer.analyze_project(root_path, project_id="global_test", tenant_id=tenant_id)

    # 4. Resolve Links (Global + Network)
    print("Step 2: Resolving Network Links...")
    N_URI = env_get("NEO4J_URI", default="bolt://localhost:7687")
    N_USER = env_get("NEO4J_USER", default="neo4j")
    N_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]
    
    resolver = XRefResolver(uri=N_URI, user=N_USER, password=N_PASS)
    stats = resolver.run_all(tenant_id)
    print(f"Resolution stats: {stats}")
    resolver.close()

    # 5. VERIFICATION in Neo4j
    print("Step 3: Verifying Graph Connectivity...")
    driver = GraphDatabase.driver(N_URI, auth=(N_USER, N_PASS))
    with driver.session() as session:
        # Busca o link NETWORK_RESOLVES_TO entre os dois projetos
        query = """
        MATCH (c:Call {tenant_id: $tid})-[r:NETWORK_RESOLVES_TO]->(f:Function {tenant_id: $tid})
        RETURN c.file_path as caller, f.file as target, f.url_endpoint as endpoint
        """
        records = list(session.run(query, tid=tenant_id))
        if records:
            for rec in records:
                print(f"[FOUND] Global Link: {rec['caller']} -> {rec['target']} (Endpoint: {rec['endpoint']})")
            print("[SUCCESS] Global Connectivity verified!")
        else:
            print("[FAILURE] Global Link not found.")
            # Debug: check nodes
            f_nodes = session.run("MATCH (f:Function {tenant_id: $tid}) RETURN f.name, f.url_endpoint", tid=tenant_id)
            print("Functions in graph:", list(f_nodes))
            c_nodes = session.run("MATCH (c:Call {tenant_id: $tid}) RETURN c.name, c.args_content", tid=tenant_id)
            print("Calls in graph:", list(c_nodes))

    driver.close()

if __name__ == "__main__":
    asyncio.run(test_global_connectivity())
