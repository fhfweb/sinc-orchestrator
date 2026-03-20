param(
    [string]$ProjectPath = "g:\Fernando\project0",
    [int]$NumTasks = 200
)

$orchestratorRoot = Join-Path $ProjectPath "ai-orchestrator"
$taskDagPath = Join-Path $orchestratorRoot "tasks/task-dag.json"
$schedulerScript = Join-Path $ProjectPath "scripts\v2\Invoke-SchedulerV2.ps1"

# Backup original DAG
$backupPath = $taskDagPath + ".bak"
Copy-Item $taskDagPath $backupPath -Force

# Create large dummy DAG
$tasks = New-Object System.Collections.Generic.List[object]
for ($i = 1; $i -le $NumTasks; $i++) {
    $tasks.Add(@{
        id = "BENCH-TASK-$i"
        description = "Benchmark dummy task number $i"
        priority = "P$(Get-Random -Minimum 0 -Maximum 4)"
        dependencies = @()
        status = "pending"
        files_affected = @("src/file$i.txt")
        created_at = (Get-Date).ToString("s")
        updated_at = (Get-Date).ToString("s")
    })
}

$dag = @{
    generated_at = (Get-Date).ToString("s")
    tasks = $tasks
} | ConvertTo-Json -Depth 20
[System.IO.File]::WriteAllText($taskDagPath, $dag)

Write-Host "--- Running Benchmark for $NumTasks tasks ---" -ForegroundColor Cyan
$elapsed = Measure-Command {
    & powershell -File $schedulerScript -ProjectPath $ProjectPath -MaxAssignmentsPerRun 10 -EmitJson | Out-Null
}

Write-Host "Execution Time: $($elapsed.TotalSeconds) seconds" -ForegroundColor Yellow

# Restore backup
Move-Item $backupPath $taskDagPath -Force
