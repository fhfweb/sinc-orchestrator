import os
from services.ast_analyzer import ASTAnalyzer

def test_polyglot_hub():
    print("--- Testing Polyglot Hub Architecture ---")
    
    # Create temp files for testing
    os.makedirs("tmp_polyglot", exist_ok=True)
    
    with open("tmp_polyglot/test.py", "w") as f:
        f.write('class PythonClass:\n    """Doc Python"""\n    def method(self):\n        pass\n')
        
    with open("tmp_polyglot/test.js", "w") as f:
        f.write('/** Doc JS */\nclass JSClass {\n    login() {}\n}\n')

    analyzer = ASTAnalyzer()
    # We won't run full analyze_project to avoid Neo4j side effects here, 
    # but we can test the internal dispatching.
    
    from services.ast_analyzer import _get_parser
    
    print("Testing Python Driver Dispatch...")
    py_driver = _get_parser("python")
    py_symbols = py_driver.parse(open("tmp_polyglot/test.py").read(), "test.py")
    print(f"Python Symbol Count: {len(py_symbols['classes'])} classes, {len(py_symbols['functions'])} fns")
    assert len(py_symbols['classes']) == 1
    assert "Python" in py_symbols['classes'][0]['name']

    print("Testing Framework Intelligence (FastAPI)...")
    with open("tmp_polyglot/app.py", "w") as f:
        f.write('from fastapi import FastAPI\napp = FastAPI()\n@app.get("/health")\ndef health(): return {"ok": True}')
    py_symbols = py_driver.parse(open("tmp_polyglot/app.py").read(), "app.py")
    metadata = py_symbols.get("framework_metadata", {})
    print(f"FastAPI Routes Found: {len(metadata.get('fastapi_routes', []))}")
    assert len(metadata.get('fastapi_routes', [])) == 1
    assert "WebAPI" in py_symbols.get("tags", [])

    print("Testing Framework Intelligence (React)...")
    js_driver = _get_parser("javascript")
    with open("tmp_polyglot/Component.tsx", "w") as f:
        f.write('import React, {useState} from "react";\nexport function MyComp() {\n  const [s, setS] = useState(0);\n  return <div />;\n}')
    js_symbols = js_driver.parse(open("tmp_polyglot/Component.tsx").read(), "Component.tsx")
    metadata = js_symbols.get("framework_metadata", {})
    print(f"React Hooks Found: {metadata.get('react_hooks', [])}")
    assert "useState" in metadata.get('react_hooks', [])
    assert "ReactComponent" in js_symbols.get("tags", [])

    print("[SUCCESS] Polyglot Hub & Framework Intelligence Verified!")

if __name__ == "__main__":
    try:
        test_polyglot_hub()
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback
        traceback.print_exc()
