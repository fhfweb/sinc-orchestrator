from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional

class BaseParser(ABC):
    """
    Abstract Base Class for all language parsers in SINC.
    Following SOLID principles for Enterprise-grade extensibility.
    """

    @abstractmethod
    def parse(self, content: str, rel_path: str) -> Dict[str, Any]:
        """
        Parses source code and returns a symbol dictionary.
        Must return a dict with: classes, functions, imports, calls, assignments, complexity_total.
        """
        pass

    def get_supported_extensions(self) -> List[str]:
        """Returns a list of file extensions supported by this driver."""
        return []

    def _count_complexity(self, node) -> int:
        """Helper to count branch nodes if using Tree-sitter."""
        return 1
