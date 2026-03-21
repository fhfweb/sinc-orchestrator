class ImpactAnalyzer:
    """
    Calculates the 'Blast Radius' of a change to a symbol.
    Traverses the graph upstream (who depends on me) and downstream (what I depend on).
    """
    def __init__(self, driver):
        self.driver = driver

    def analyze_impact(self, target_name: str, project_id: str, tenant_id: str, max_depth: int = 3):
        """
        Identify callers and importers at different depths.
        """
        with self.driver.session() as session:
            # Query upstream impact (Who breaks if I change?)
            result = session.run("""
                MATCH (start {name: $name, project_id: $pid, tenant_id: $tid})
                MATCH path = (affected)-[:CALLS|IMPORTS|EXTENDS|IMPLEMENTS*1..$depth]->(start)
                RETURN 
                    affected.name as name, 
                    affected.file as file, 
                    labels(affected) as labels,
                    length(path) as depth
                ORDER BY depth ASC
            """, name=target_name, pid=project_id, tid=tenant_id, depth=max_depth)
            
            impacts = []
            for record in result:
                impacts.append({
                    "name": record["name"],
                    "file": record["file"],
                    "type": record["labels"][0] if record["labels"] else "Unknown",
                    "depth": record["depth"],
                    "risk": "HIGH" if record["depth"] == 1 else "MEDIUM" if record["depth"] == 2 else "LOW"
                })
            
            return {
                "target": target_name,
                "total_affected": len(impacts),
                "impact_map": impacts
            }
