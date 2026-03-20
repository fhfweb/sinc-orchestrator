import os
import time
from services.ast_analyzer import ASTAnalyzer
from services.xref_resolver import XRefResolver
from services.taint_tracker import TaintTracker

def test_data_lineage():
    print("--- Testing Data Lineage & Taint Analysis (Phase 13) ---")
    project_path = "tmp_lineage"
    os.makedirs(project_path, exist_ok=True)
    tenant_id = f"test_lineage_{int(time.time())}"

    # 1. Create a 3-layer data flow
    # Layer 1: Controller (Source)
    with open(f"{project_path}/controller.py", "w") as f:
        f.write('''
@app.post("/users")
def create_user(email: str):
    from service import UserService
    UserService.save(email)
''')
    
    # Layer 2: Service
    with open(f"{project_path}/service.py", "w") as f:
        f.write('''
class UserService:
    def save(data: str):
        from db import DB
        DB.execute_sql(data)
''')

    # Layer 3: Database (Sink)
    with open(f"{project_path}/db.py", "w") as f:
        f.write('''
class DB:
    def execute_sql(sql_query: str):
        # SINK: database operation
        print("Executing:", sql_query)
''')

    # 2. Run AST Analysis
    print("Step 1: AST Analysis (Identifying Sources/Sinks)...")
    with ASTAnalyzer() as analyzer:
        analyzer.analyze_project(project_path, project_id="lineage_test", tenant_id=tenant_id)

    # 3. Run XRef Resolution (Connecting Files)
    print("Step 2: Global XRef Resolution...")
    from services.streaming.core.config import env_get
    N_URI = env_get("NEO4J_URI", default="bolt://localhost:7687")
    N_USER = env_get("NEO4J_USER", default="neo4j")
    N_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]
    
    resolver = XRefResolver(uri=N_URI, user=N_USER, password=N_PASS)
    resolver.run_all(tenant_id)
    resolver.close()

    # 4. Run Taint Tracking
    print("Step 3: Data Lineage Pathfinding...")
    tracker = TaintTracker(uri=N_URI, user=N_USER, password=N_PASS)
    lineages = tracker.find_all_lineages(tenant_id)
    
    if lineages:
        for lin in lineages:
            print(f"[FOUND] Path from {lin['source']} to {lin['sink']}:")
            print(f" -> " + " -> ".join(lin['path']))
    else:
        print("[FAILED] No lineage found.")

    assert len(lineages) > 0
    tracker.close()
    print("[SUCCESS] Data Lineage verified! Maximum depth reached.")

if __name__ == "__main__":
    try:
        test_data_lineage()
    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback; traceback.print_exc()
