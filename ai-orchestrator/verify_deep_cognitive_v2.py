import os
import json
from services.ast_analyzer import _get_parser

def test_deep_cognitive():
    print("--- Testing Deep Cognitive Specialization (Phase 10) ---")
    os.makedirs("tmp_deep", exist_ok=True)

    # 1. Test NestJS Deep Analysis
    with open("tmp_deep/user.controller.ts", "w") as f:
        f.write('import { Controller, Get, Post } from "@nestjs/common";\n'
                'import { UserService } from "./user.service";\n'
                '@Controller("users")\n'
                'export class UserController {\n'
                '  constructor(private readonly service: UserService) {}\n'
                '  @Get(":id")\n'
                '  async findOne() {}\n'
                '  @Post()\n'
                '  async create() {}\n'
                '}')
    js_driver = _get_parser("typescript")
    js_symbols = js_driver.parse(open("tmp_deep/user.controller.ts").read(), "user.controller.ts")
    meta = js_symbols.get("framework_metadata", {})
    print(f"NestJS Metadata: {json.dumps(meta, indent=2)}")
    assert meta["controller_base"] == "users"
    assert len(meta["endpoints"]) == 2
    assert meta["endpoints"][0]["path"] == "users/:id"
    assert "UserService" in meta["injected_services"]

    # 2. Test Django Deep Analysis
    with open("tmp_deep/models.py", "w") as f:
        f.write('from django.db import models\n'
                'class Product(models.Model):\n'
                '    name = models.CharField(max_length=255)\n'
                '    price = models.DecimalField(max_digits=10, decimal_places=2)\n'
                '    category = models.ForeignKey("Category", on_delete=models.CASCADE)\n')
    py_driver = _get_parser("python")
    py_symbols = py_driver.parse(open("tmp_deep/models.py").read(), "models.py")
    meta = py_symbols.get("framework_metadata", {})
    print(f"Django Metadata: {json.dumps(meta, indent=2)}")
    product_fields = meta["django_models"]["Product"]["fields"]
    assert any(f["name"] == "name" and f["type"] == "CharField" for f in product_fields)
    assert any(f["name"] == "category" and f["type"] == "ForeignKey" for f in product_fields)

    # 3. Test AI Deep Analysis
    with open("tmp_deep/agent.py", "w") as f:
        f.write('from langchain.tools import tool\n'
                'from langchain.prompts import PromptTemplate\n'
                '# Isso demonstra uma LCEL Chain com LLM\n'
                '@tool\n'
                'def search_db(query: str): pass\n'
                'chain = PromptTemplate.from_template("...") | ChatOpenAI() | StrOutputParser()')
    ai_symbols = py_driver.parse(open("tmp_deep/agent.py").read(), "agent.py")
    meta = ai_symbols.get("framework_metadata", {})
    print(f"AI Metadata: {json.dumps(meta, indent=2)}")
    assert "search_db" in meta["ai_tools"]
    assert meta["chain_type"] == "LCEL"

    print("[SUCCESS] Deep Cognitive Specialization Verified! Not superficial anymore.")

if __name__ == "__main__":
    try:
        test_deep_cognitive()
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback; traceback.print_exc()
