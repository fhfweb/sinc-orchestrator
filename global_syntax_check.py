import os
import py_compile
import sys

service_dir = r"g:\Fernando\project0\ai-orchestrator\services"
files = [f for f in os.listdir(service_dir) if f.endswith(".py")]

errors = []
for f in files:
    full_path = os.path.join(service_dir, f)
    try:
        py_compile.compile(full_path, doraise=True)
        print(f"OK: {f}")
    except Exception as e:
        print(f"ERROR: {f} -> {e}")
        errors.append(f)

if errors:
    print(f"\nTotal Errors: {len(errors)}")
    sys.exit(1)
else:
    print("\nGlobal Syntax Check: PASSED")
