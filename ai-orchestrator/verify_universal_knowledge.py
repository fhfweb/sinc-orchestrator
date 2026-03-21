import os
from services.ast_analyzer import ASTAnalyzer, _get_parser

def test_universal_knowledge():
    print("--- Testing Universal Knowledge Hub ---")
    os.makedirs("tmp_universal", exist_ok=True)
    
    # 1. Test Python (Django + AI)
    with open("tmp_universal/models.py", "w") as f:
        f.write('from django.db import models\nfrom langchain import OpenAI\nclass UserProfile(models.Model): pass')
    py_driver = _get_parser("python")
    py_symbols = py_driver.parse(open("tmp_universal/models.py").read(), "models.py")
    tags = py_symbols.get("tags", [])
    print(f"Python Tags: {tags}")
    assert "DjangoModel" in tags
    assert "LangChain" in tags
    assert "AI/ML" in py_symbols.get("tags", []) # From manager.py generic check

    # 2. Test JS/TS (NestJS + Next.js)
    with open("tmp_universal/app.controller.ts", "w") as f:
        f.write('import { Controller, Get } from "@nestjs/common";\n@Controller()\nexport class AppController {}')
    js_driver = _get_parser("typescript") or _get_parser("javascript")
    js_symbols = js_driver.parse(open("tmp_universal/app.controller.ts").read(), "app.controller.ts")
    tags = js_symbols.get("tags", [])
    print(f"JS Tags: {tags}")
    assert "NestJS-Controller" in tags

    # 3. Test Infra (Docker + K8s)
    with open("tmp_universal/Dockerfile", "w") as f:
        f.write('FROM python:3.9\nCOPY . /app\nCMD ["python", "app.py"]')
    infra_driver = _get_parser("infra")
    infra_symbols = infra_driver.parse(open("tmp_universal/Dockerfile").read(), "Dockerfile")
    tags = infra_symbols.get("tags", [])
    print(f"Docker Tags: {tags}")
    assert "Docker" in tags

    with open("tmp_universal/deploy.yaml", "w") as f:
        f.write('apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: agent-sinc')
    k8s_symbols = infra_driver.parse(open("tmp_universal/deploy.yaml").read(), "deploy.yaml")
    tags = k8s_symbols.get("tags", [])
    print(f"K8s Tags: {tags}")
    assert "Kubernetes" in tags

    # 4. Test Enterprise (Spring Boot + .NET)
    with open("tmp_universal/App.java", "w") as f:
        f.write('@SpringBootApplication\npublic class App { }')
    ent_driver = _get_parser("enterprise")
    ent_symbols = ent_driver.parse(open("tmp_universal/App.java").read(), "App.java")
    print(f"Java Tags: {ent_symbols.get('tags', [])}")
    assert "Spring-Boot" in ent_symbols.get("tags", [])

    with open("tmp_universal/Api.cs", "w") as f:
        f.write('[ApiController]\npublic class ApiController { }')
    cs_symbols = ent_driver.parse(open("tmp_universal/Api.cs").read(), "Api.cs")
    print(f"C# Tags: {cs_symbols.get('tags', [])}")
    assert "ASP.NET-Core" in cs_symbols.get("tags", [])

    print("[SUCCESS] Universal Knowledge Hub (Full Spectrum) Verified!")

if __name__ == "__main__":
    try:
        test_universal_knowledge()
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback; traceback.print_exc()
