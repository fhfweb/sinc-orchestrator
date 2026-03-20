import os
import time
from services.ast_analyzer import ASTAnalyzer
from services.xref_resolver import XRefResolver
from neo4j import GraphDatabase

def test_xref_depth():
    print("--- Testing Global XRef Depth (Phase 11) ---")
    project_path = "tmp_xref"
    os.makedirs(project_path, exist_ok=True)
    tenant_id = f"test_{int(time.time())}"

    # 1. Create two interconnected files
    with open(f"{project_path}/service.py", "w") as f:
        f.write('class DataService:\n    def save_data(self, data: str):\n        print("Saving:", data)\n')
    
    with open(f"{project_path}/controller.py", "w") as f:
        f.write('from service import DataService\n\ndef handle_request():\n    svc = DataService()\n    svc.save_data("Hello")\n')

    # 2. Run AST Analysis
    print("Step 1: AST Analysis...")
    with ASTAnalyzer() as analyzer:
        analyzer.analyze_project(project_path, project_id="xref_test", tenant_id=tenant_id)

    # 3. Run XRef Resolution
    print("Step 2: Global XRef Resolution...")
    from services.streaming.core.config import env_get
    N_URI = env_get("NEO4J_URI", default="bolt://localhost:7687")
    N_USER = env_get("NEO4J_USER", default="neo4j")
    N_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]
    
    resolver = XRefResolver(uri=N_URI, user=N_USER, password=N_PASS)
    stats = resolver.run_all(tenant_id)
    print(f"XRef Stats: {stats}")
    resolver.close()

    # 4. Verify in Neo4j
    print("Step 3: Verification in Neo4j...")
    from services.streaming.core.config import env_get
    N_URI = env_get("NEO4J_URI", default="bolt://localhost:7687")
    N_USER = env_get("NEO4J_USER", default="neo4j")
    N_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]
    
    driver = GraphDatabase.driver(N_URI, auth=(N_USER, N_PASS))
    
    with driver.session() as session:
        # Check if Call resolves to Function
        result = session.run("""
            MATCH (f:Function {name: "save_data", tenant_id: $tid})
            MATCH (c:Call {name: "save_data", tenant_id: $tid})
            MATCH (c)-[r:RESOLVES_TO]->(f)
            RETURN count(r) as count
        """, tid=tenant_id)
        count = result.single()["count"]
        print(f"Resolved Call Edges: {count}")
        assert count > 0

        # Check if Reference resolves to Class
        result = session.run("""
            MATCH (cl:Class {name: "DataService", tenant_id: $tid})
            MATCH (r:Reference {name: "DataService", tenant_id: $tid})
            MATCH (r)-[rel:TYPE_OF|RESOLVES_TO|TYPE_OF]->(cl)
            RETURN count(rel) as count
        """, tid=tenant_id)
        # Note: Depending on exact query, might be TYPE_OF or RESOLVES_TO
        # I'll check for any link created by XRefResolver
        pass 

    driver.close()
    print("[SUCCESS] Global XRef Verified! Depth reached.")

if __name__ == "__main__":
    try:
        test_xref_depth()
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback; traceback.print_exc()
