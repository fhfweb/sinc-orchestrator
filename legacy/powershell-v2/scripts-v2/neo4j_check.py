from neo4j import GraphDatabase
import sys

uri = "bolt://localhost:7687"
user = "neo4j"
password = "6c887da889bce4c756657f2e2c2f712be66fbcce099cc6de"

def main():
    try:
        driver = GraphDatabase.driver(uri, auth=(user, password))
        with driver.session() as session:
            count_nodes = session.run("MATCH (n) RETURN count(n) as count").single()["count"]
            count_rels = session.run("MATCH ()-[r]->() RETURN count(r) as count").single()["count"]
            print(f"Nodes: {count_nodes}")
            print(f"Relationships: {count_rels}")
            
            # Group by label
            labels = session.run("MATCH (n) RETURN labels(n)[0] as label, count(n) as count ORDER BY count DESC")
            print("\nNodes per label:")
            for record in labels:
                print(f"  {record['label']}: {record['count']}")
                
        driver.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
