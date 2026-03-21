import os
import shutil
from services.ast_analyzer import ASTAnalyzer

# Mock Neo4j driver
class MockTx:
    def run(self, query, **kwargs):
        # print(f"QUERY: {query[:50]}... ARGS: {kwargs}")
        pass

class MockSession:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def execute_write(self, fn, *args):
        fn(MockTx(), *args)

class MockDriver:
    def session(self): return MockSession()
    def close(self): pass

def setup_mock_project(root):
    os.makedirs(os.path.join(root, "pkg_a"), exist_ok=True)
    os.makedirs(os.path.join(root, "pkg_b"), exist_ok=True)
    
    with open(os.path.join(root, "pkg_a/logic.py"), "w") as f:
        f.write("def do_something():\n    pass\n")
        
    with open(os.path.join(root, "pkg_b/main.py"), "w") as f:
        f.write("from pkg_a.logic import do_something\n")
        f.write("def run():\n    do_something()\n")

def test_ast_analyzer_resolution():
    project_root = "/tmp/mock_ast_project"
    if os.path.exists(project_root):
        shutil.rmtree(project_root)
    setup_mock_project(project_root)
    
    analyzer = ASTAnalyzer()
    analyzer._driver = MockDriver()
    
    # We want to catch the progress to see the symbols
    processed_symbols = {}
    def on_progress(path, symbols):
        processed_symbols[path] = symbols
        print(f"  Parsed {path}")
    
    result = analyzer.analyze_project(project_root, project_id="test", tenant_id="local", on_progress=on_progress)
    
    print("\n--- TEST RESULTS ---")
    print(f"Files processed: {result['files']}")
    
    # Check if 'do_something' call in pkg_b/main.py was resolved to pkg_a/logic.py
    main_symbols = processed_symbols.get("pkg_b/main.py")
    if main_symbols:
        resolved_calls = main_symbols.get("resolved_calls", [])
        print(f"Resolved calls in main.py: {resolved_calls}")
        
        found = any(c["name"] == "do_something" and c["target_file"] == "pkg_a/logic.py" for c in resolved_calls)
        if found:
            print("✓ Cross-file call resolution successful!")
        else:
            print("✗ Cross-file call resolution FAILED.")
    
    # Check if imports were resolved
    resolved_imports = main_symbols.get("resolved_imports", [])
    print(f"Resolved imports in main.py: {resolved_imports}")
    if "pkg_a/logic.py" in resolved_imports:
        print("✓ Import resolution successful!")
    else:
        print("✗ Import resolution FAILED.")

if __name__ == "__main__":
    try:
        test_ast_analyzer_resolution()
    except Exception as e:
        print(f"ERROR: {e}")
