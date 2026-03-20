from neo4j import GraphDatabase
import json

uri = "bolt://localhost:7687"
user = "neo4j"
password = "6c887da889bce4c756657f2e2c2f712be66fbcce099cc6de"

def run_query(session, query, **params):
    return session.run(query, **params)

def main():
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        print("### SINC Knowledge Graph Analysis Report\n")
        
        # 1. Total Stats
        stats = session.run("MATCH (n) RETURN count(n) as nodes, count(labels(n)) as labels").single()
        print(f"- Total Nodes: {stats['nodes']}")
        
        # 2. Node Distribution
        print("\n- Node Labels Distribution:")
        dist = session.run("MATCH (n) UNWIND labels(n) as label RETURN label, count(*) as count ORDER BY count DESC")
        for r in dist:
            print(f"  - {r['label']}: {r['count']}")
            
        # 3. CRM Modules Analysis
        print("\n- CRM Related Elements (Keywords: Lead, Contact):")
        crm = session.run("""
            MATCH (n) 
            WHERE n.id CONTAINS 'crm' OR n.summary CONTAINS 'Lead' OR n.summary CONTAINS 'Contact'
            RETURN n.node_label as type, count(*) as count 
            ORDER BY count DESC
        """)
        for r in crm:
            print(f"  - {r['type']}: {r['count']}")
            
        # 4. Agenda Modules Analysis
        print("\n- Agenda Related Elements (Keywords: Agenda, Appointment, Schedule):")
        agenda = session.run("""
            MATCH (n) 
            WHERE n.id CONTAINS 'agenda' OR n.summary CONTAINS 'Agenda' OR n.summary CONTAINS 'Appointment' OR n.summary CONTAINS 'Schedule'
            RETURN n.node_label as type, count(*) as count 
            ORDER BY count DESC
        """)
        for r in agenda:
            print(f"  - {r['type']}: {r['count']}")
            
        # 5. Potential Intersections (Relationships between CRM and Agenda)
        print("\n- Potential CRM <-> Agenda Intersections:")
        inter = session.run("""
            MATCH (c:MemoryNode)-[r]->(a:MemoryNode)
            WHERE (c.id CONTAINS 'crm' OR c.summary CONTAINS 'Lead' OR c.summary CONTAINS 'Contact')
              AND (a.id CONTAINS 'agenda' OR a.summary CONTAINS 'Agenda' OR a.summary CONTAINS 'Appointment')
            RETURN c.summary as source, type(r) as rel, a.summary as target
        """)
        for r in inter:
            print(f"  - [{r['source']}] --{r['rel']}--> [{r['target']}]")
            
        # 6. Route mapping
        print("\n- Top Routes (Critical Paths):")
        routes = session.run("MATCH (r:MemoryNode) WHERE r.node_type = 'route' RETURN r.summary as route LIMIT 10")
        for r in routes:
            print(f"  - {r['route']}")

    driver.close()

if __name__ == "__main__":
    main()
