import os

runner_path = r"g:\Fernando\project0\ai-orchestrator\services\local_agent_runner.py"

with open(runner_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
# 1. Add imports and cache at the top (after import asyncio at line 38)
for i, line in enumerate(lines):
    new_lines.append(line)
    if "import asyncio" in line and i < 50:
        new_lines.append("import hashlib\n")
        new_lines.append("EMBEDDING_CACHE = {} # L0 Local Cache to mitigate 'Double-Dipping' costs\n")

# 2. Remove the mess at the former line 2637
final_lines = []
skip = False
for line in new_lines:
    if 'import hashlib' in line and 'elif name == "spawn_agent":' in final_lines[-1] if final_lines else False:
        skip = True
        continue
    if skip and ('import time' in line or 'EMBEDDING_CACHE = {}' in line or line.strip() == ""):
        if 'import time' in line and 'import time' not in [l.strip() for l in final_lines]:
             # Keep it if it's the only one, but actually it's already there
             continue
        if 'EMBEDDING_CACHE' in line:
            continue
        if line.strip() == "":
            continue
    skip = False
    final_lines.append(line)

# Wait, the above logic is a bit complex. Let's just do a string replacement on the whole content.
content = "".join(lines)

# Remove the broken block
broken_block = """            import hashlib
import time

EMBEDDING_CACHE = {} # L0 Local Cache to mitigate 'Double-Dipping' costs as _time
"""
content = content.replace(broken_block, "")

# Add imports at top
content = content.replace("import asyncio", "import asyncio\nimport hashlib\n\nEMBEDDING_CACHE = {} # L0 Local Cache\n")

with open(runner_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Cleaned up local_agent_runner.py")
