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
                file_path = record["file"] or ""
                labels_raw = record["labels"] or []
                depth = record["depth"]
                
                # Baseline Risk Map
                risk = "HIGH" if depth == 1 else "MEDIUM" if depth == 2 else "LOW"
                impact_type = labels_raw[0] if labels_raw else "Unknown"

                # Cognitive Risk Overrides (Red Team Threat Matrix)
                fp_lower = file_path.lower()
                if "/routes/" in fp_lower or "api" in fp_lower:
                    risk = "CRITICAL_ROUTE_BREAK"
                    impact_type = "API Endpoint"
                elif "db" in fp_lower or "redis" in fp_lower or "qdrant" in fp_lower or "http_client" in fp_lower:
                    risk = "CRITICAL_STATE_BREAK"
                    impact_type = "Data Sink/External"
                elif "core/" in fp_lower:
                    risk = "CRITICAL_CORE_DEPENDENCY"
                    impact_type = "Kernel Module"

                impacts.append({
                    "name": record["name"],
                    "file": file_path,
                    "type": impact_type,
                    "depth": depth,
                    "risk": risk
                })
            
            return {
                "target": target_name,
                "total_affected": len(impacts),
                "impact_map": impacts
            }
