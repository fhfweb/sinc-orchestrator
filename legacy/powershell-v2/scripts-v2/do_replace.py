import os
import glob
import re

replacements = {
    "Initialize-V2Directory": "Initialize-V2Directory",
    "Remove-V2ExpiredLocks": "Remove-V2ExpiredLocks",
    "New-V2TaskLocks": "New-V2TaskLocks",
    "Remove-V2TaskLocks": "Remove-V2TaskLocks",
    "Add-V2MarkdownLog": "Add-V2MarkdownLog",
    "Initialize-V2PhaseApprovals": "Initialize-V2PhaseApprovals",
    "Add-RepairTask": "Add-RepairTask",
    "Test-OrchestratorDependencies": "Test-OrchestratorDependencies",
    "Repair-Agent": "Repair-Agent",
    "Send-SystemEvent": "Send-SystemEvent"
}

def process_directory(directory):
    for root, dirs, files in os.walk(directory):
        for file in files:
            if file.endswith('.ps1') or file.endswith('.py'):
                filepath = os.path.join(root, file)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                new_content = content
                for old, new in replacements.items():
                    # Use regex to do case-insensitive replacement with word boundaries if needed,
                    # but since PowerShell is case-insensitive, we should replace exact matches (case-insensitive)
                    pattern = re.compile(re.escape(old), re.IGNORECASE)
                    new_content = pattern.sub(new, new_content)
                
                # Tech debt specific items inside Common.ps1
                if file.lower() == 'common.ps1':
                    new_content = re.sub(r'\$Profile\b', '$V2Profile', new_content, flags=re.IGNORECASE)

                if content != new_content:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    print(f"Updated {filepath}")

process_directory('g:/Fernando/project0/scripts/v2')
