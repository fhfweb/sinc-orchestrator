from typing import Dict, Any, List
from services.frameworks.fastapi_profile import FastAPIProfile
from services.frameworks.react_profile import ReactProfile
from services.frameworks.python.django_profile import DjangoProfile
from services.frameworks.python.ai_profile import AIProfile
from services.frameworks.js.nestjs_profile import NestJSProfile
from services.frameworks.infra_profile import InfraProfile
from services.frameworks.enterprise_profile import EnterpriseProfile
from services.frameworks.go_profile import GoProfile
from services.frameworks.mobile_game_profile import MobileGameProfile

class ProfileManager:
    """
    Despacha o conteúdo dos arquivos para os perfis de inteligência adequados.
    """
    
    @staticmethod
    def analyze_python(content: str, symbols: Dict[str, Any]):
        FastAPIProfile.identify(content, symbols)
        DjangoProfile.identify(content, symbols)
        AIProfile.identify(content, symbols)

    @staticmethod
    def analyze_js(content: str, symbols: Dict[str, Any], rel_path: str):
        ReactProfile.identify(content, symbols)
        NestJSProfile.identify(content, symbols)
        MobileGameProfile.identify(content, symbols)
        
        # Next.js check (in-line for now)
        if "next" in content.lower() and ("pages" in rel_path or "app" in rel_path):
             symbols["tags"] = list(set(symbols.get("tags", []) + ["Next.js", "Fullstack"]))

    @staticmethod
    def analyze_go(content: str, symbols: Dict[str, Any]):
        GoProfile.identify(content, symbols)

    @staticmethod
    def analyze_enterprise(content: str, symbols: Dict[str, Any]):
        """Suporte para Java/C# e linguagens corporativas."""
        EnterpriseProfile.identify(content, symbols)
        MobileGameProfile.identify(content, symbols) # Unity (C#)

    @staticmethod
    def analyze_infra(filename: str, content: str, symbols: Dict[str, Any]):
        InfraProfile.identify(filename, content, symbols)
