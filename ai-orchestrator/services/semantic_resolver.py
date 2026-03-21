import os
from typing import Dict, List, Set, Optional, Any
from dataclasses import dataclass, field

@dataclass
class SymbolDefinition:
    """Represents a defined symbol in the codebase."""
    name: str
    file_path: str
    kind: str  # 'Class', 'Function', 'Method', 'Interface', 'Variable', etc.
    line: int
    fqn: Optional[str] = None
    owner_id: Optional[str] = None  # ID of the enclosing class
    bases: List[str] = field(default_factory=list) # List of parent class names
    return_type: Optional[str] = None
    node_id: Optional[str] = None  # Neo4j Node ID if already upserted

class SuffixIndex:
    """
    Reverse index of file paths based on their suffixes.
    Allows resolving 'services.auth' to 'services/auth.py' efficiently.
    """
    def __init__(self, file_paths: List[str]):
        self.index: Dict[str, List[str]] = {}
        for path in file_paths:
            normalized = path.replace("\\", "/")
            parts = normalized.split("/")
            # Add all suffixes (e.g., 'auth.py', 'services/auth.py')
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                if suffix not in self.index:
                    self.index[suffix] = []
                self.index[suffix].append(path)

    def resolve(self, query: str) -> List[str]:
        """Find files matching the suffix query."""
        query = query.replace(".", "/").replace("\\", "/")
        # Try exact suffix match or adding extension
        candidates = self.index.get(query, [])
        if not candidates:
            for ext in [".py", ".ts", ".js", ".php", ".go"]:
                candidates = self.index.get(query + ext, [])
                if candidates:
                    break
        return candidates

class SymbolTable:
    """Global registry of all symbols extracted during the first pass."""
    def __init__(self):
        self.symbols: Dict[str, List[SymbolDefinition]] = {}
        self.file_symbols: Dict[str, Set[str]] = {}

    def add(self, symbol: SymbolDefinition):
        if symbol.name not in self.symbols:
            self.symbols[symbol.name] = []
        self.symbols[symbol.name].append(symbol)
        
        if symbol.file_path not in self.file_symbols:
            self.file_symbols[symbol.file_path] = set()
        self.file_symbols[symbol.file_path].add(symbol.name)

    def lookup(self, name: str, file_hint: Optional[str] = None) -> List[SymbolDefinition]:
        """Find symbols by name, optionally filtering by file."""
        candidates = self.symbols.get(name, [])
        if file_hint:
            # Prioritize symbols in the same file
            same_file = [c for c in candidates if c.file_path == file_hint]
            if same_file:
                return same_file
        return candidates

    def clear(self):
        self.symbols.clear()
        self.file_symbols.clear()

class ResolutionContext:
    """Context for resolving expressions and symbols within a session."""
    def __init__(self, symbols: SymbolTable, suffix_index: SuffixIndex):
        self.symbols = symbols
        self.suffix_index = suffix_index
        self.import_map: Dict[str, Set[str]] = {}  # file_path -> Set[resolved_file_paths]

    def add_import(self, from_file: str, raw_path: str):
        resolved = self.suffix_index.resolve(raw_path)
        if resolved:
            if from_file not in self.import_map:
                self.import_map[from_file] = set()
            for r in resolved:
                self.import_map[from_file].add(r)

    def resolve_symbol(self, name: str, from_file: str) -> List[SymbolDefinition]:
        """ Tiered resolution: Same File > Imports > Global """
        # Tier 1: Same File
        same_file = [s for s in self.symbols.lookup(name) if s.file_path == from_file]
        if same_file:
            return same_file

        # Tier 2: Directly Imported Files
        imported_files = self.import_map.get(from_file, set())
        imported_symbols = [s for s in self.symbols.lookup(name) if s.file_path in imported_files]
        if imported_symbols:
            return imported_symbols

        # Tier 3: Global (Fallback)
        return self.symbols.lookup(name)

    def get_mro(self, class_name: str, from_file: str) -> List[SymbolDefinition]:
        """
        Calculates the Method Resolution Order for a class.
        Simplified version of C3 linearization.
        """
        mro = []
        visited = set()
        queue = self.resolve_symbol(class_name, from_file)
        
        while queue:
            current = queue.pop(0)
            if current.node_id in visited:
                continue
            mro.append(current)
            visited.add(current.node_id or f"{current.file_path}:{current.name}")
            
            # Add parents to queue
            for base in current.bases:
                parents = self.resolve_symbol(base, current.file_path)
                queue.extend(parents)
        
        return mro


class TypeUniverse:
    """
    Heuristic type inference engine. 
    Tracks assignments within a file scope to guess the type of a variable.
    """
    def __init__(self):
        # file_path -> { var_name -> type_name }
        self.assignments: Dict[str, Dict[str, str]] = {}

    def record_assignment(self, file_path: str, var_name: str, type_name: str):
        if file_path not in self.assignments:
            self.assignments[file_path] = {}
        self.assignments[file_path][var_name] = type_name

    def infer_type(self, file_path: str, var_name: str) -> Optional[str]:
        return self.assignments.get(file_path, {}).get(var_name)
