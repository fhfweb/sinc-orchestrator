import os
import time
import asyncio
from services.ast_analyzer import ASTAnalyzer
from services.semantic_annotator import SemanticAnnotator
from neo4j import GraphDatabase

async def test_gohorse_intelligence():
    print("--- Testing GoHorse Intelligence (Phase 12) ---")
    project_path = "tmp_gohorse"
    os.makedirs(project_path, exist_ok=True)
    tenant_id = f"test_xgh_{int(time.time())}"

    # 1. Create a "GoHorse" file with bad nomenclature
    # Function x123 is clearly a DB saver but has a random name
    with open(f"{project_path}/chaos.py", "w") as f:
        f.write('''
def x123(p_id, p_val):
    """XGH style function"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO data (id, val) VALUES (?, ?)", (p_id, p_val))
    conn.commit()
''')

    # 2. Run AST Analysis
    print("Step 1: Running Deep Analysis...")
    with ASTAnalyzer() as analyzer:
        analyzer.analyze_project(project_path, project_id="gohorse_test", tenant_id=tenant_id)

    # 3. Verify labels in Neo4j
    print("Step 2: Verifying Behavioral Tags in Neo4j...")
    from services.streaming.core.config import env_get
    N_URI = env_get("NEO4J_URI", default="bolt://localhost:7687")
    N_USER = env_get("NEO4J_USER", default="neo4j")
    N_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]
    
    driver = GraphDatabase.driver(N_URI, auth=(N_USER, N_PASS))
    
    with driver.session() as session:
        result = session.run("""
            MATCH (f:Function {name: "x123", tenant_id: $tid})
            RETURN f.tags as tags
        """, tid=tenant_id)
        record = result.single()
        tags = record["tags"] if record else []
        print(f"Detected Tags for 'x123': {tags}")
        
        assert "GOHORSE_WARNING" in tags
        assert "BEHAVIOR:DB_MUTATION" in tags

    # 4. Semantic Intent via LLM
    print("Step 3: Inferring Semantic Intent via LLM...")
    annotator = SemanticAnnotator()
    code = open(f"{project_path}/chaos.py").read()
    intent = await annotator.infer_intent(code)
    print(f"Inferred Real Intent: {intent}")
    
    # We expect something like 'Database Persistence' or 'Insert Data'
    assert intent != "Inference Failed"
    
    annotator.close()
    driver.close()
    print("[SUCCESS] GoHorse de-obfuscated! Intent identified.")

if __name__ == "__main__":
    asyncio.run(test_gohorse_intelligence())
