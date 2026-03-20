
import os
import json
import urllib.request
from neo4j import GraphDatabase

# Verified ports from docker ps
NEO4J_URI  = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "6c887da889bce4c756657f2e2c2f712be66fbcce099cc6de"

QDRANT_HOST = "localhost"
QDRANT_PORT = 6333

PROJECT_ID = "sinc"
TENANT_ID  = "local"

def init_neo4j():
    print(f"Initializing Neo4j Schema at {NEO4J_URI}...")
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        with driver.session() as session:
            # 1. Constraints/Indexes
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (a:Agent) REQUIRE a.name IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (t:Task) REQUIRE t.id IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Solution) REQUIRE s.id IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Error) REQUIRE e.fingerprint IS UNIQUE")
            
            # Additional indexes for potency
            session.run("CREATE INDEX IF NOT EXISTS FOR (f:File) ON (f.path)")
            session.run("CREATE INDEX IF NOT EXISTS FOR (f:File) ON (f.tenant_id)")
            
            print("Schema constraints and indexes created.")
        driver.close()
    except Exception as e:
        print(f"Neo4j Init Failed: {e}")

def init_qdrant():
    print(f"Initializing Qdrant Collections at {QDRANT_PORT}...")
    collections = [
        f"{TENANT_ID}_{PROJECT_ID}_solutions",
        f"{TENANT_ID}_{PROJECT_ID}_errors",
        f"{TENANT_ID}_{PROJECT_ID}_agent_behaviors",
        f"{TENANT_ID}_{PROJECT_ID}_code_chunks"
    ]
    
    for coll in collections:
        url = f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{coll}"
        # nomic-embed-text is 768
        payload = json.dumps({
            "vectors": {
                "size": 768,
                "distance": "Cosine"
            }
        }).encode()
        
        try:
            req = urllib.request.Request(url, data=payload, method="PUT",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"Created collection: {coll}")
        except Exception as e:
            print(f"Collection {coll} might already exist or error: {e}")

if __name__ == "__main__":
    init_neo4j()
    init_qdrant()
