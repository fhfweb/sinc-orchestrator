import os
import time
from services.ast_analyzer import ASTAnalyzer
from services.xref_resolver import XRefResolver
from services.streaming.core.config import env_get

def run_global_analysis():
    print("--- 🌍 SINC Global Analysis: Phase 16 ---")
    root_path = "g:/Fernando/project0"
    tenant_id = "global_ecosystem"
    
    # 1. Neo4j Creds
    N_URI = env_get("NEO4J_URI", default="bolt://localhost:7687")
    N_USER = env_get("NEO4J_USER", default="neo4j")
    N_PASS = env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]

    # 2. Deep AST Analysis
    print(f"Step 1: Analyzing entire root {root_path}...")
    with ASTAnalyzer() as analyzer:
        stats = analyzer.analyze_project(root_path, project_id="ecosystem", tenant_id=tenant_id)
        print(f"Analysis complete: {stats}")

    # 3. Global XRef Resolution
    print("Step 2: Resolving Global Cross-Project Symbols...")
    resolver = XRefResolver(uri=N_URI, user=N_USER, password=N_PASS)
    resolver.run_all(tenant_id)
    resolver.close()

    print("[SUCCESS] Ecosystem analyzed. You can now query cross-repo dependencies.")

if __name__ == "__main__":
    run_global_analysis()
