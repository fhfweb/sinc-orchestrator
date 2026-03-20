import os

root_dir = r"g:\Fernando\project0\scripts\v2"
orch_dir = r"g:\Fernando\project0\ai-orchestrator\scripts\v2"

root_files = set(os.listdir(root_dir))
orch_files = set(os.listdir(orch_dir))

only_in_root = root_files - orch_files
only_in_orch = orch_files - root_files

print(f"Only in root: {only_in_root}")
print(f"Only in orch: {only_in_orch}")

for f in only_in_root:
    if os.path.isfile(os.path.join(root_dir, f)):
        print(f"File to move: {f}")
