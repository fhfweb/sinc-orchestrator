from neo4j import GraphDatabase
import os
from services.graph_intelligence import GraphIntelligenceService

def check_gds():
    gi = GraphIntelligenceService()
    driver = gi._get_driver()
    with driver.session() as session:
        try:
            result = session.run("RETURN gds.version() as version")
            record = result.single()
            print(f"GDS Version: {record['version']}")
        except Exception as e:
            print(f"GDS not found or error: {e}")
    gi.close()

if __name__ == "__main__":
    check_gds()
