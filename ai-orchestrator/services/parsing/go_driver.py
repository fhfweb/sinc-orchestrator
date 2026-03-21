import re
from typing import Dict, Any, List
from services.parsing.base_driver import BaseParser
from services.parsing.ts_utils import get_parser, count_complexity
from services.frameworks.manager import ProfileManager

class GoParser(BaseParser):
    def get_supported_extensions(self) -> List[str]:
        return [".go"]

    def parse(self, content: str, rel_path: str) -> Dict[str, Any]:
        parser = get_parser("go")
        if parser:
            symbols = self._parse_ts(content, rel_path, parser)
        else:
            symbols = self._parse_regex(content, rel_path)
            
        # Enterprise Capability: Universal Domain Knowledge
        ProfileManager.analyze_go(content, symbols)
        return symbols

    def _parse_ts(self, content: str, rel_path: str, parser) -> Dict[str, Any]:
        symbols: Dict[str, Any] = {"classes": [], "functions": [], "imports": [], "calls": [], "assignments": [], "complexity_total": 0}
        try:
            tree = parser.parse(content.encode("utf-8", errors="replace"))
            root = tree.root_node
            # Funcs
            for node in root.children:
                if node.type in ("function_declaration", "method_declaration"):
                    name_node = node.child_by_field_name("name")
                    rec_node = node.child_by_field_name("receiver")
                    body_node = node.child_by_field_name("body")
                    if name_node:
                        name = name_node.text.decode("utf-8", errors="replace")
                        receiver = rec_node.text.decode("utf-8", errors="replace") if rec_node else None
                        cc = count_complexity(body_node) if body_node else 1
                        symbols["functions"].append({
                            "name": name, "receiver": receiver, "complexity": cc,
                            "type": "method" if receiver else "function",
                            "line": node.start_point[0] + 1
                        })
                        symbols["complexity_total"] += cc
            # Structs (as classes)
            for m in re.finditer(r"^type\s+(\w+)\s+struct", content, re.MULTILINE):
                symbols["classes"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1})
        except: return self._parse_regex(content, rel_path)
        return symbols

    def _parse_regex(self, content: str, rel_path: str) -> Dict[str, Any]:
        symbols = {"classes": [], "functions": [], "imports": [], "calls": [], "assignments": [], "complexity_total": 0}
        for m in re.finditer(r"^func\s+(?:\([^\)]+\)\s+)?(\w+)\s*\(", content, re.MULTILINE):
            symbols["functions"].append({"name": m.group(1), "line": content[:m.start()].count("\n") + 1, "type": "function"})
        return symbols
