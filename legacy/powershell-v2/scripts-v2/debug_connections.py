import socket
import os
import urllib.request

def check_port(host, port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        result = s.connect_ex((host, port))
        s.close()
        return result == 0
    except Exception as e:
        return f"Error: {e}"

targets = [
    ("localhost", 8765),
    ("db", 3306),
    ("sinc-db", 3306),
    ("neo4j", 7687),
    ("sinc-neo4j", 7687),
    ("qdrant", 6333),
    ("sinc-qdrant", 6333),
    ("ollama", 11434),
    ("sinc-ollama", 11434),
]

print("--- Connectivity Test ---")
for host, port in targets:
    status = check_port(host, port)
    print(f"{host}:{port} -> {'UP' if status is True else 'DOWN' if status is False else status}")

print("\n--- Environment Variables ---")
for env in ["DB_HOST", "DB_NAME", "DB_USER", "NEO4J_URI", "NEO4J_USER"]:
    print(f"{env}={os.getenv(env)}")

print("\n--- Local Endpoints (Direct) ---")
for path in ["/health", "/clinical/intelligence", "/graph/meta"]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8765{path}", timeout=3) as r:
            print(f"GET {path} -> SUCCESS ({r.status})")
    except Exception as e:
        print(f"GET {path} -> FAILED: {e}")
        if hasattr(e, 'read'):
             print(f"Body: {e.read().decode()}")
