Set-StrictMode -Version Latest

Describe "V2 Scheduler blocked-startup gate" {
    It "does not assign non-recovery tasks when project status is blocked-startup" {
        $fixtureRoot = Join-Path $PSScriptRoot "..\..\workspace\tmp\v2-scheduler-gate-test"
        $fixtureRoot = [System.IO.Path]::GetFullPath($fixtureRoot)
        if (Test-Path -LiteralPath $fixtureRoot) {
            Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
        }

        New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $fixtureRoot "ai-orchestrator\state") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $fixtureRoot "ai-orchestrator\tasks") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $fixtureRoot "ai-orchestrator\locks") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $fixtureRoot "ai-orchestrator\agents") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $fixtureRoot "ai-orchestrator\communication") -Force | Out-Null

        $state = @{
            status = "blocked-startup"
        } | ConvertTo-Json -Depth 8
        Set-Content -LiteralPath (Join-Path $fixtureRoot "ai-orchestrator\state\project-state.json") -Value $state -Encoding UTF8

        $taskDag = @{
            generated_at = "2026-03-08T00:00:00"
            updated_at   = "2026-03-08T00:00:00"
            tasks        = @(
                @{
                    id             = "V2-PLAN-001"
                    description    = "Plan architecture"
                    priority       = "P0"
                    dependencies   = @()
                    preferred_agent = "AI Architect"
                    status         = "pending"
                    files_affected = @("ai-orchestrator/documentation/architecture.md")
                }
            )
        } | ConvertTo-Json -Depth 12
        Set-Content -LiteralPath (Join-Path $fixtureRoot "ai-orchestrator\tasks\task-dag.json") -Value $taskDag -Encoding UTF8

        $workload = @{
            generated_at = "2026-03-08T00:00:00"
            agents       = @(
                @{ name = "AI Architect"; active_tasks = 0; max_parallel_tasks = 2; status = "available" }
            )
        } | ConvertTo-Json -Depth 12
        Set-Content -LiteralPath (Join-Path $fixtureRoot "ai-orchestrator\agents\workload.json") -Value $workload -Encoding UTF8

        Set-Content -LiteralPath (Join-Path $fixtureRoot "ai-orchestrator\locks\locks.json") -Value '{"locks":[]}' -Encoding UTF8

        $schedulerScript = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\scripts\v2\Invoke-SchedulerV2.ps1"))
        $output = & powershell -ExecutionPolicy Bypass -File $schedulerScript -ProjectPath $fixtureRoot -EmitJson
        $result = ($output | Out-String) | ConvertFrom-Json

        $result.scheduled | Should Be 0
    }
}
