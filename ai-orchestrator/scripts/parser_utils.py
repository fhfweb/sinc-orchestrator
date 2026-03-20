import tree_sitter
from tree_sitter import Language, Parser

class ParserPool:
    _instance = None
    _languages = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ParserPool, cls).__new__(cls)
        return cls._instance

    def get_language(self, lang_id):
        if lang_id in self._languages:
            return self._languages[lang_id]
        
        try:
            if lang_id == "python":
                import tree_sitter_python as tspy
                lang = Language(tspy.language())
            elif lang_id == "php":
                import tree_sitter_php as tsphp
                # Try language_php first (v0.24+)
                if hasattr(tsphp, "language_php"):
                    lang = Language(tsphp.language_php())
                else:
                    lang = Language(tsphp.language())
            elif lang_id == "javascript":
                import tree_sitter_javascript as tsjs
                lang = Language(tsjs.language())
            else:
                return None
            
            self._languages[lang_id] = lang
            return lang
        except Exception:
            return None

    def get_parser(self, lang_id):
        lang = self.get_language(lang_id)
        if lang:
            return Parser(lang)
        return None

def query_symbols(tree, lang_id, source_code):
    """Generic query for classes and functions/methods."""
    parser_pool = ParserPool()
    lang = parser_pool.get_language(lang_id)
    if not lang:
        return [], []

    # Simplified queries for common patterns
    if lang_id == "python":
        q = """
        (class_definition name: (identifier) @class.name)
        (function_definition name: (identifier) @func.name)
        """
    elif lang_id == "php":
        q = """
        (class_declaration name: (name) @class.name)
        (method_declaration name: (name) @func.name)
        (function_definition name: (name) @func.name)
        """
    elif lang_id == "javascript":
        q = """
        (class_declaration name: (identifier) @class.name)
        (function_declaration name: (identifier) @func.name)
        (method_definition name: (property_identifier) @func.name)
        """
    else:
        return [], []

    query = lang.query(q)
    captures = query.captures(tree.root_node)
    
    classes = []
    functions = []
    
    for node, tag in captures:
        name = source_code[node.start_byte:node.end_byte]
        if tag.startswith("class"):
            classes.append(name)
        elif tag.startswith("func"):
            functions.append(name)
            
    return sorted(list(set(classes))), sorted(list(set(functions)))
