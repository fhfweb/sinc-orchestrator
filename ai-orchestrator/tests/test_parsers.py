import sys
import os
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from services.local_agent_runner import _analyze_code_impl

def test_php_parser():
    php_code = """<?php
namespace App\Services;
class AuthService extends BaseService {
    public function login(Request $request) {
        // nested logic
        if ($auth) { return true; }
    }
    
    private function validateHash($hash) {
        return password_verify('...', $hash);
    }
}

interface AuthInterface {
    public function logout();
}
"""
    result = _analyze_code_impl("app/Services/AuthService.php", php_code)
    print("\nPHP Analysis Result:")
    print(f"Classes: {[c['name'] for c in result['classes']]}")
    print(f"Functions: {[f['name'] for f in result['functions']]}")
    print(f"Parser Used: {result.get('parser', 'legacy')}")
    
    assert "AuthService" in [c['name'] for c in result['classes']]
    assert "login" in [f['name'] for f in result['functions']]
    assert "validateHash" in [f['name'] for f in result['functions']]
    assert result.get('parser') == "tree-sitter"

def test_js_parser():
    js_code = """
class Dashboard extends React.Component {
    render() {
        return <div>Hello</div>;
    }
    
    fetchData = async () => {
        const res = await fetch('/api/stats');
    }
}

function GlobalUtil() {
    console.log("Global");
}
"""
    result = _analyze_code_impl("resources/js/Dashboard.jsx", js_code)
    print("\nJS/JSX Analysis Result:")
    print(f"Classes: {[c['name'] for c in result['classes']]}")
    print(f"Functions: {[f['name'] for f in result['functions']]}")
    
    assert "Dashboard" in [c['name'] for c in result['classes']]
    assert "render" in [f['name'] for f in result['functions']]
    assert "GlobalUtil" in [f['name'] for f in result['functions']]

if __name__ == "__main__":
    try:
        test_php_parser()
        test_js_parser()
        print("\nALL PARSER TESTS PASSED!")
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
