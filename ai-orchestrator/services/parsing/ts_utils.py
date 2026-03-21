import logging

log = logging.getLogger("orch.parsing")

_HAS_TS = False
_TS_PARSERS: dict = {}

_CC_NODES = frozenset({
    "if_statement", "elif_clause", "for_statement", "while_statement",
    "with_statement", "except_clause", "assert_statement",
    "conditional_expression", "boolean_operator",
    "else_clause", "for_in_statement", "do_statement", "switch_case",
    "catch_clause", "ternary_expression", "logical_expression",
    "select_statement", "type_switch_statement", "case_clause"
})

def ts_init():
    global _HAS_TS, _TS_PARSERS
    if _HAS_TS: return
    try:
        from tree_sitter import Parser, Language
        def _try_lang(pkg, fn):
            try:
                mod = __import__(pkg)
                func = getattr(mod, fn, None) or getattr(mod, "language", None)
                return Language(func())
            except: return None
        
        langs = {
            "python": _try_lang("tree_sitter_python", "language"),
            "javascript": _try_lang("tree_sitter_javascript", "language"),
            "typescript": _try_lang("tree_sitter_typescript", "language_typescript"),
            "go": _try_lang("tree_sitter_go", "language"),
            "php": _try_lang("tree_sitter_php", "language_php"),
        }
        for l, obj in langs.items():
            if obj: _TS_PARSERS[l] = Parser(obj)
        _HAS_TS = bool(_TS_PARSERS)
    except: pass

def get_parser(lang: str):
    ts_init()
    return _TS_PARSERS.get(lang)

def count_complexity(node) -> int:
    if not node: return 0
    count = 1 if node.type in _CC_NODES else 0
    for child in node.children:
        count += count_complexity(child)
    return count
