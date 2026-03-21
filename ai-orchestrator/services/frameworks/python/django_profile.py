import re
from typing import Dict, Any

class DjangoProfile:
    """
    Identifica padrões profundos do framework Django em arquivos Python.
    Extrai informações de modelos, campos e relacionamentos.
    """
    @staticmethod
    def identify(content: str, symbols: Dict[str, Any]):
        tags = []
        framework_metadata = symbols.get("framework_metadata", {})
        
        # 1. Extração de Campos de Modelos
        if "from django.db import models" in content or "(models.Model)" in content:
            tags.append("DjangoModel")
            
            # Busca por classes de modelo e seus campos
            models_data = {}
            class_matches = re.finditer(r"class\s+(\w+)\s*\((?:models\.Model|Model)\):", content)
            for cm in class_matches:
                model_name = cm.group(1)
                # Pega o corpo da classe (heurística: até o próximo 'class' ou def no nível 0)
                body = content[cm.end():]
                next_class = re.search(r"^\w", body, re.MULTILINE)
                if next_class: body = body[:next_class.start()]
                
                fields = []
                # Regex para pegar campos: name = models.CharField(...)
                field_pattern = r"^\s+(\w+)\s*=\s*models\.(\w+Field|ForeignKey|OneToOneField|ManyToManyField)\("
                for fm in re.finditer(field_pattern, body, re.MULTILINE):
                    fields.append({"name": fm.group(1), "type": fm.group(2)})
                
                models_data[model_name] = {"fields": fields}
            
            if models_data:
                framework_metadata["django_models"] = models_data
                tags.append("ORM-Django")
        
        if "urlpatterns" in content and "path(" in content:
            tags.append("DjangoRouting")

        if tags:
            symbols["tags"] = list(set(symbols.get("tags", []) + tags))
            symbols["framework_metadata"] = framework_metadata
