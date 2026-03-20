import os
import time
import asyncio
from services.ast_analyzer import ASTAnalyzer
from services.xref_resolver import XRefResolver
from services.code_healer import CodeHealer
from neo4j import GraphDatabase

async def test_self_healing():
    print("--- Testing Self-Healing & Autonomous Refactoring (Phase 14) ---")
    project_path = "tmp_healing"
    os.makedirs(project_path, exist_ok=True)
    tenant_id = f"test_healing_{int(time.time())}"

    # 1. Create GoHorse code
    # Definition in logic.py
    with open(f"{project_path}/logic.py", "w") as f:
        f.write('''
def xgh_auth_001(u, p):
    """Obscure auth function"""
    if u == "admin" and p == "123":
        return True
    return False
''')
    
    # Call in main.py
    with open(f"{project_path}/main.py", "w") as f:
        f.write('''
from logic import xgh_auth_001
def run():
    if xgh_auth_001("user", "pass"):
        print("Logged in")
''')

    # 2. Run Analysis
    print("Step 1: Running Initial Analysis...")
    with ASTAnalyzer() as analyzer:
        analyzer.analyze_project(project_path, project_id="healing_test", tenant_id=tenant_id)

    # 3. Run XRef to link call to function
    print("Step 2: Linking XRefs...")
    from services.streaming.core.config import env_get
    N_URI = env_get("NEO4J_URI", default="bolt://localhost:7687")
    N_USER = env_get("NEO4J_USER", default="neo4j")
    N_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]
    
    resolver = XRefResolver(uri=N_URI, user=N_USER, password=N_PASS)
    resolver.run_all(tenant_id)
    resolver.close()

    # 4. Simulate Semantic Labeling (Goal: User Authentication)
    print("Step 3: Simulating Semantic Labeling...")
    driver = GraphDatabase.driver(N_URI, auth=(N_USER, N_PASS))
    with driver.session() as session:
        session.run("""
            MATCH (f:Function {name: "xgh_auth_001", tenant_id: $tid})
            SET f.semantic_intent = "User Authentication"
        """, tid=tenant_id)

    # 5. Execute HEALING
    print("Step 4: Executing SELF-HEALING...")
    healer = CodeHealer(uri=N_URI, user=N_USER, password=N_PASS)
    candidates = healer.get_healing_candidates(tenant_id)
    print(f"Candidates found: {len(candidates)}")
    
    if candidates:
        c = candidates[0]
        new_name = healer.suggest_professional_name(c["intent"])
        print(f"Curando {c['old_name']} -> {new_name}")
        healer.apply_healing(c["id"], new_name, project_path)
    
    healer.close()

    # 6. VERIFICATION
    print("Step 5: Verifying Healing Results...")
    # Check logic.py
    with open(f"{project_path}/logic.py", "r") as f:
        logic_code = f.read()
        print(f"New logic.py content:\n{logic_code}")
        assert "def user_authentication" in logic_code
    
    # Check main.py (Cross-file Update)
    with open(f"{project_path}/main.py", "r") as f:
        main_code = f.read()
        print(f"New main.py content:\n{main_code}")
        # Note: CodeHealer currently renames strings, so it should catch calls
        assert "user_authentication" in main_code

    print("[SUCCESS] Self-Healing verified! GoHorse cured.")
    driver.close()

if __name__ == "__main__":
    asyncio.run(test_self_healing())
