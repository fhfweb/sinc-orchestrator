from typing import Dict, Any

class MobileGameProfile:
    """
    Identifica padrões de Mobile (Flutter, React Native) e Games (Unity, Unreal).
    """
    @staticmethod
    def identify(content: str, symbols: Dict[str, Any]):
        tags = []
        # Flutter (Dart) - Embora analisado como texto puro por enquanto
        if "package:flutter/" in content:
            tags.append("Flutter")
            tags.append("Mobile")
            
        # React Native
        if 'from "react-native"' in content or "import 'react-native'" in content:
            tags.append("React-Native")
            tags.append("Mobile")

        # Games
        if "UnityEngine" in content:
            tags.append("Unity")
            tags.append("Game-Dev")
        if "UCLASS()" in content or "GENERATED_BODY()" in content:
            tags.append("Unreal-Engine")
            tags.append("Game-Dev-C++")

        if tags:
            symbols["tags"] = list(set(symbols.get("tags", []) + tags))
