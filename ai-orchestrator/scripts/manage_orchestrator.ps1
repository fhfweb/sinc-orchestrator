# SINC Orchestrator Management CLI (PostgreSQL Edition)
# Version: 1.2.0
param (
    [Parameter(Mandatory=$true)]
    [ValidateSet('takeover', 'visualize', 'audit', 'unlock-all', 'task-info')]
    [string]$Action,
    [string]$TaskId,
    [string]$Reason = 'Manual'
)

$PythonBridge = 'G:\Fernando\project0\workspace\projects\SINC\ai-orchestrator\scripts\v2\orchestrator_db_bridge.py'
$ReportPath = 'G:\Fernando\project0\workspace\projects\SINC\ai-orchestrator\reports\dag_visualization.md'

function Get-Dag {
    $raw = python $PythonBridge list
    return $raw | ConvertFrom-Json
}

if ($Action -eq 'takeover') {
    if (-not $TaskId) { Write-Error 'TaskId required'; exit 1 }
    Write-Output ('Taking over ' + $TaskId + ' as Antigravity')
    python $PythonBridge update --task-id $TaskId --status 'in-progress' --agent 'Antigravity'
    Write-Output 'DONE.'
}
elseif ($Action -eq 'unlock-all') {
    python $PythonBridge unlock-all
}
elseif ($Action -eq 'task-info') {
    if (-not $TaskId) { Write-Error 'TaskId required'; exit 1 }
    python $PythonBridge info --task-id $TaskId
}
elseif ($Action -eq 'visualize') {
    $Dag = Get-Dag
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine('# DAG Visualization (PostgreSQL)')
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('Generated at: ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    [void]$sb.AppendLine('')
    [void]$sb.AppendLine('```mermaid')
    [void]$sb.AppendLine('graph TD')

    foreach ($T in $Dag.tasks) {
        $Color = 'fill:#fff'
        if ($T.status -eq 'done') { $Color = 'fill:#8f8,stroke:#080' }
        elseif ($T.status -eq 'in-progress') { $Color = 'fill:#8cf,stroke:#00f' }
        elseif ($T.status -match 'blocked') { $Color = 'fill:#f88,stroke:#f00' }
        
        if ($T.critical_path -eq $true) {
            $Color = 'fill:#f96,stroke:#f60,stroke-width:2px'
        }

        $idSafe = $T.id -replace '[^a-zA-Z0-9]', '_'
        $label = $T.id + ': ' + $T.status
        
        $node = '    ' + $idSafe + '["' + $label + '"]'
        [void]$sb.AppendLine($node)
        
        $style = '    style ' + $idSafe + ' ' + $Color
        [void]$sb.AppendLine($style)
        
        foreach ($D in $T.dependencies) {
            $depSafe = $D -replace '[^a-zA-Z0-9]', '_'
            $link = '    ' + $depSafe + ' --> ' + $idSafe
            [void]$sb.AppendLine($link)
        }
    }
    [void]$sb.AppendLine('```')
    
    [System.IO.File]::WriteAllText($ReportPath, $sb.ToString())
    Write-Output 'Visualization generated.'
}
elseif ($Action -eq 'audit') {
    $Dag = Get-Dag
    $limit = (Get-Date).AddMinutes(-30)
    foreach ($T in $Dag.tasks) {
        if ($T.status -eq 'in-progress') {
            $upd = [DateTime]$T.updated_at
            if ($upd -lt $limit) {
                Write-Output ('STALE: ' + $T.id + ' (Last: ' + $T.updated_at + ')')
            }
        }
    }
}
