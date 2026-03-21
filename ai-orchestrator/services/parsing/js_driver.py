import re
from typing import Dict, Any, List, Optional
from services.parsing.base_driver import BaseParser
from services.parsing.ts_utils import get_parser, count_complexity
from services.frameworks.manager import ProfileManager

class JSParser(BaseParser):
    def get_supported_extensions(self) -> List[str]:
        return [".js", ".ts", ".jsx", ".tsx"]

    def parse(self, content: str, rel_path: str) -> Dict[str, Any]:
        lang = "typescript" if rel_path.endswith((".ts", ".tsx")) else "javascript"
        parser = get_parser(lang) or get_parser("javascript")
        if parser:
            symbols = self._parse_ts(content, rel_path, parser, lang)
        else:
            symbols = self._parse_regex(content, rel_path)
            
        # 1. Enterprise Capability: Universal Domain Knowledge
        ProfileManager.analyze_js(content, symbols, rel_path)
        
        # 2. Phase 12 & 13: GoHorse & Data Lineage (Source/Sink Detection)
        for fn in symbols.get("functions", []):
            name = fn["name"]
            # GoHorse Detection
            if len(name) < 4 or re.match(r"^[a-z][0-9]{1,3}$", name) or "xgh" in name.lower():
                fn["tags"] = fn.get("tags", []) + ["GOHORSE_WARNING"]
            
            # Data Lineage: SOURCE Identification (Heurística: Decoradores NestJS/Express)
            fn_block = content.splitlines()[fn["line"]-3:fn["line"]+3] 
            block_text = "\n".join(fn_block).lower()
            if any(dec in block_text for dec in ["@get()", "@post()", "@body()", "@query()", "@param()"]):
                fn["tags"] = fn.get("tags", []) + ["DATA_SOURCE"]
                # Extrai endpoint: ex @Get("/users") -> /users
                ep_match = re.search(r'["\'](/[^"\']+)["\']', block_text)
                if ep_match:
                    fn["url_endpoint"] = ep_match.group(1)
            
            # Data Lineage: SINK Identification
            if any(sink in block_text for sink in ["save(", "update(", "insert(", "remove(", "collection("]):
                fn["tags"] = fn.get("tags", []) + ["DATA_SINK"]

            # Behavioral Fingerprinting (Legacy P12)
            if "db" in content.lower() or "collection" in content.lower():
                fn["tags"] = fn.get("tags", []) + ["BEHAVIOR:DB_ACCESS"]
            if "fetch" in content or "axios" in content or "http" in content:
                fn["tags"] = fn.get("tags", []) + ["BEHAVIOR:NETWORK_IO"]
                    
        return symbols

    def _parse_ts(self, content: str, rel_path: str, parser, lang: str) -> Dict[str, Any]:
        symbols: Dict[str, Any] = {
            "classes": [], "functions": [], "imports": [], "calls": [],
            "assignments": [], "complexity_total": 0
        }
        try:
            tree = parser.parse(content.encode("utf-8", errors="replace"))
            def walk_scope(node, scope_classes=None):
                if scope_classes is None: scope_classes = []
                node_type = node.type

                if node_type in ("class_declaration", "class_expression"):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        cls_name = name_node.text.decode("utf-8", errors="replace")
                        symbols["classes"].append({
                            "name": cls_name, "line": node.start_point[0] + 1, "line_end": node.end_point[0] + 1
                        })
                        for child in node.children: walk_scope(child, scope_classes + [cls_name])
                        return

                if node_type in ("function_declaration", "function_expression", "arrow_function", "method_definition"):
                    name_node = node.child_by_field_name("name")
                    body_node = node.child_by_field_name("body")
                    name = name_node.text.decode("utf-8", errors="replace") if name_node else "<anonymous>"
                    cc = count_complexity(body_node) if body_node else 1
                    owner = scope_classes[-1] if scope_classes else None
                    docstring = ""
                    if node_type == "method_definition":
                        prev = node.prev_sibling
                        if prev and prev.type == "comment":
                            docstring = prev.text.decode("utf-8", errors="replace").strip("/* \n")
                    symbols["functions"].append({
                        "name": name, "owner": owner, "complexity": cc, "docstring": docstring[:500],
                        "line": node.start_point[0] + 1, "line_end": node.end_point[0] + 1,
                        "type": "method" if (node_type == "method_definition" or owner) else "function"
                    })
                    symbols["complexity_total"] += cc
                    for child in node.children: walk_scope(child, scope_classes)
                    return

                if node_type == "import_statement":
                    for child in node.children:
                        if child.type == "string":
                            symbols["imports"].append(child.text.decode("utf-8", errors="replace").strip("'\""))

                if node_type == "call_expression":
                    fn_node = node.child_by_field_name("function")
                    arg_node = node.child_by_field_name("arguments")
                    if fn_node:
                        name = fn_node.text.decode("utf-8", errors="replace")
                        args = arg_node.text.decode("utf-8", errors="replace") if arg_node else ""
                        symbols["calls"].append({
                            "name": name,
                            "args_content": args
                        })

                if node_type in ("variable_declarator", "assignment_expression"):
                    right = node.child_by_field_name("value") or node.child_by_field_name("right")
                    left = node.child_by_field_name("name") or node.child_by_field_name("left")
                    if left and right and right.type == "new_expression":
                        con_node = right.child_by_field_name("constructor")
                        if con_node:
                            symbols["assignments"].append({
                                "var": left.text.decode("utf-8", errors="replace"),
                                "type": con_node.text.decode("utf-8", errors="replace")
                            })

                for child in node.children: walk_scope(child, scope_classes)

            walk_scope(tree.root_node)
        except Exception as e:
            return self._parse_regex(content, rel_path)
        return symbols

    def _parse_regex(self, content: str, rel_path: str) -> Dict[str, Any]:
        """Legacy regex-based parser for JS/TS."""
        symbols = {"classes": [], "functions": [], "imports": [], "calls": [], "assignments": [], "complexity_total": 0}
        for m in re.finditer(r"^\s*import.*from\s+['\"](.*)['\"]", content, re.MULTILINE):
            symbols["imports"].append(m.group(1))
        for m in re.finditer(r"^\s*class\s+(\w+)", content, re.MULTILINE):
            symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
        for m in re.finditer(r"^\s*function\s+(\w+)\s*\(", content, re.MULTILINE):
            symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "function"})
        return symbols
