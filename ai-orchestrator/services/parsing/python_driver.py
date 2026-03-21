import re
from typing import Dict, Any, List, Optional
from services.parsing.base_driver import BaseParser
from services.parsing.ts_utils import get_parser, count_complexity
from services.frameworks.manager import ProfileManager

class PythonParser(BaseParser):
    def get_supported_extensions(self) -> List[str]:
        return [".py"]

    def parse(self, content: str, rel_path: str) -> Dict[str, Any]:
        parser = get_parser("python")
        if parser:
            symbols = self._parse_ts(content, rel_path, parser)
        else:
            symbols = self._parse_regex(content, rel_path)
            
        # 1. Enterprise Capability: Universal Domain Knowledge
        ProfileManager.analyze_python(content, symbols)
        
        # 2. Phase 12 & 13: GoHorse & Data Lineage (Source/Sink Detection)
        for fn in symbols.get("functions", []):
            name = fn["name"]
            if len(name) < 4 or re.match(r"^[a-z][0-9]{1,3}$", name) or "xgh" in name.lower():
                fn["tags"] = fn.get("tags", []) + ["GOHORSE_WARNING"]
            
            fn_block = content.splitlines()[fn["line"]-2:fn["line"]+5] 
            block_text = "\n".join(fn_block).lower()
            if any(dec in block_text for dec in ["@get", "@post", "@put", "@delete", "@api", "@app."]):
                fn["tags"] = fn.get("tags", []) + ["DATA_SOURCE"]
                # Extrai endpoint: ex @app.get("/users") -> /users
                ep_match = re.search(r'["\'](/[^"\']+)["\']', block_text)
                if ep_match:
                    fn["url_endpoint"] = ep_match.group(1)
            
            if "execute" in block_text or "save" in block_text or "commit" in block_text:
                fn["tags"] = fn.get("tags", []) + ["DATA_SINK"]
                
        return symbols

    def _parse_ts(self, content: str, rel_path: str, parser) -> Dict[str, Any]:
        symbols: Dict[str, Any] = {
            "classes": [], "functions": [], "imports": [], 
            "calls": [], "assignments": [], "references": [],
            "complexity_total": 0
        }
        try:
            tree = parser.parse(content.encode("utf-8", errors="replace"))
            root = tree.root_node
            
            def walk(node, scope_classes=None, scope_funcs=None):
                if scope_classes is None: scope_classes = []
                if scope_funcs is None: scope_funcs = []
                nt = node.type

                if nt == "class_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        cls_name = name_node.text.decode("utf-8", errors="replace")
                        symbols["classes"].append({
                            "name": cls_name,
                            "line": node.start_point[0] + 1, "line_end": node.end_point[0] + 1
                        })
                        body = node.child_by_field_name("body")
                        if body:
                            for c in body.children: walk(c, scope_classes + [cls_name], scope_funcs)
                        return

                if nt == "function_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        f_name = name_node.text.decode("utf-8", errors="replace")
                        cc = count_complexity(node.child_by_field_name("body")) or 1
                        owner = scope_classes[-1] if scope_classes else None
                        symbols["functions"].append({
                            "name": f_name,
                            "line": node.start_point[0] + 1, "line_end": node.end_point[0] + 1,
                            "type": "method" if owner else "function",
                            "owner": owner, "complexity": cc
                        })
                        symbols["complexity_total"] += cc
                        body = node.child_by_field_name("body")
                        if body:
                            for c in body.children: walk(c, scope_classes, scope_funcs + [f_name])
                        return

                if nt == "call":
                    fn = node.child_by_field_name("function")
                    if fn:
                        c_name = ""
                        if fn.type == "identifier":
                            c_name = fn.text.decode("utf-8", errors="replace")
                        elif fn.type == "attribute":
                            attr = fn.child_by_field_name("attribute")
                            if attr: c_name = attr.text.decode("utf-8", errors="replace")
                        
                        if c_name:
                            args = node.child_by_field_name("arguments")
                            args_text = args.text.decode("utf-8", errors="replace") if args else ""
                            symbols["calls"].append({
                                "name": c_name,
                                "parent_function": scope_funcs[-1] if scope_funcs else None,
                                "args_content": args_text
                            })

                if nt in ["import_statement", "import_from_statement"]:
                    symbols["imports"].append(node.text.decode("utf-8", errors="replace"))
                
                for child in node.children:
                    walk(child, scope_classes, scope_funcs)

            walk(root)
        except Exception as e:
            return self._parse_regex(content, rel_path)
        return symbols

    def _parse_regex(self, content: str, rel_path: str) -> Dict[str, Any]:
        symbols = {"classes": [], "functions": [], "imports": [], "calls": [], "assignments": [], "complexity_total": 0}
        return symbols
