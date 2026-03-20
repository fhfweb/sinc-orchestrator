from neo4j import GraphDatabase
from services.graph_intelligence import GraphIntelligenceService

def check_apoc():
    gi = GraphIntelligenceService()
    driver = gi._get_driver()
    with driver.session() as session:
        try:
            result = session.run("RETURN apoc.version() as version")
            record = result.single()
            print(f"APOC Version: {record['version']}")
        except Exception as e:
            print(f"APOC not found or error: {e}")
    gi.close()

if __name__ == "__main__":
    check_apoc()
