
import os
import sys
import time
from pathlib import Path

# Add services to path
sys.path.insert(0, str(Path(os.getcwd())))

from services.local_agent_runner import _execute_tool

WORKSPACE = Path(os.getcwd())

print("--- STEP 1: Browser Pool Performance Test ---")
t1 = time.time()
r1 = _execute_tool("take_screenshot", {"url": "https://www.google.com"}, WORKSPACE)
t2 = time.time()
print(f"First call (cold): {t2-t1:.2f}s")

r2 = _execute_tool("take_screenshot", {"url": "https://www.google.com"}, WORKSPACE)
t3 = time.time()
print(f"Second call (warm - pooled): {t3-t2:.2f}s (Should be much faster)")

print("\n--- STEP 2: Visual Masking Test ---")
# Use the same image twice but mask a random area
if "OK" in r2:
    p = r2.split("saved to ")[1].split(". URL")[0].strip()
    # Mask a large area (0,0 to 500,500) and compare to itself
    res_mask = _execute_tool("compare_screenshots", {"path1": p, "path2": p, "exclude_regions": [[0,0,500,500]]}, WORKSPACE)
    print(f"Masked Self-Comparison: {res_mask}")

print("\n--- STEP 3: Semantic Navigation Test ---")
# Try a semantic click on Google (e.g. 'Gmail' link)
res_sem = _execute_tool("browser_semantic_action", {
    "url": "https://www.google.com",
    "instruction": "click the Gmail link",
    "action_type": "click"
}, WORKSPACE)
print(f"Semantic Result: {res_sem}")
