import tree_sitter
import tree_sitter_php as tsphp
import tree_sitter_python as tspython
from tree_sitter import Language, Parser

def verify_lang(lang_module, name, code):
    try:
        lang = Language(lang_module.language())
        parser = Parser(lang)
        tree = parser.parse(code.encode("utf-8"))
        root = tree.root_node
        print(f"OK: {name} parsed. Root type: {root.type}, Children: {len(root.children)}")
        return True
    except Exception as e:
        print(f"FAIL: {name} error: {e}")
        return False

# Test Code
php_code = """<?php
class User extends Model {
    public function getBalance() { return 100; }
}
"""
py_code = """
class Agent:
    def run(self):
        pass
"""

print("Verificando Tree-sitter configurations...")
v1 = verify_lang(tsphp, "PHP", php_code)
v2 = verify_lang(tspython, "Python", py_code)

if v1 and v2:
    print("\nSUCCESS: All parsers ready.")
else:
    print("\nFAILURE: One or more parsers failed.")
