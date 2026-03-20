<#
.SYNOPSIS
    Creates the strict .ai-orchestrator/ directory layer inside a project.
.DESCRIPTION
    Initializes the complete .ai-orchestrator directory structure required by all V2 scripts:
      state/        - project-state.json, locks.json, health-report.json
      tasks/        - task-dag.json, backlog.md
      analysis/     - architecture-report.md, dependency-graph.md, code-quality.md
      docker/       - generated Docker assets
      reports/      - daily/weekly/monthly reports
      communication/- messages.md, alerts.md, handoffs.md
      knowledge_base/lessons/ - lessons learned from repairs
      projects/<slug>/  - isolated per-project memory scope
    Does not overwrite existing files unless -Force is set.
.PARAMETER ProjectPath
    Path to the project root. Defaults to current directory.
.PARAMETER Force
    If set, overwrites existing .ai-orchestrator files.
.EXAMPLE
    .\scripts\v2\Initialize-AIOrchestratorLayer.ps1 -ProjectPath C:\projects\myapp
    .\scripts\v2\Initialize-AIOrchestratorLayer.ps1 -ProjectPath C:\projects\myapp -Force
#>param(
    [string]$ProjectPath = ".",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "Common.ps1")

$resolvedProjectPath = Resolve-V2AbsolutePath -Path $ProjectPath
if (-not $resolvedProjectPath -or -not (Test-Path -LiteralPath $resolvedProjectPath -PathType Container)) {
    throw "Project path does not exist: $ProjectPath"
}

$root = Join-Path $resolvedProjectPath "ai-orchestrator"
$projectSlug = Get-V2ProjectSlug -Name (Split-Path -Leaf $resolvedProjectPath)

$directories = @(
    "core",
    "core/bootstrap",
    "core/project_detector",
    "core/docker_manager",
    "core/port_validator",
    "core/connection_manager",
    "agents",
    "memory",
    "memory/neo4j_graph",
    "memory/qdrant_vectors",
    "memory/embeddings",
    "runtime",
    "runtime/agent_scheduler",
    "runtime/dag_engine",
    "runtime/observers",
    "projects",
    "tasks",
    "analysis",
    "knowledge_base",
    "knowledge_base/lessons_learned",
    "project-pack",
    "docker",
    "database",
    "database/schema",
    "services",
    "experiments",
    "reports",
    "locks",
    "communication",
    "documentation",
    "state"
)

Initialize-V2Directory -Path $root
foreach ($directory in $directories) {
    Initialize-V2Directory -Path (Join-Path $root $directory)
}
Initialize-V2Directory -Path (Join-Path $root ("projects/{0}" -f $projectSlug))

$agentRegistry = @"
# Agent Registry

## AI CTO
- Role: architecture decisions and long-term technical strategy
- Responsibilities: approve structural decisions, enforce refactor policy gates, resolve high-impact tradeoffs
- Skills: architecture governance, risk prioritization, roadmap alignment

## AI Architect
- Role: system design authority
- Responsibilities: module boundaries, API contracts, data model integrity
- Skills: architecture patterns, service decomposition, dependency design

## AI Engineer
- Role: implementation executor
- Responsibilities: implement tasks, add tests, update docs and evidence
- Skills: backend, frontend, integrations, refactoring under constraints

## AI Security Engineer
- Role: security and compliance reviewer
- Responsibilities: vulnerability detection, permission model review, secure defaults
- Skills: threat modeling, dependency risk review, secure coding

## AI DevOps Engineer
- Role: infrastructure and delivery owner
- Responsibilities: containerization, pipeline reliability, environment isolation
- Skills: Docker, CI/CD, runtime diagnostics, observability

## AI Product Manager
- Role: product and task prioritization owner
- Responsibilities: define outcomes, acceptance criteria, delivery sequencing
- Skills: requirements framing, task decomposition, impact prioritization
"@

$memoryWorldModel = @"
# World Model

This file is canonical memory for domain entities, relationships, and behavior.

## Current Status
- Source: generated and reviewed
- Confidence: unknown
- Last Updated: $(Get-V2Timestamp)

## Entities
- pending

## Relationships
- pending

## Business Rules
- pending
"@

$memoryArchitecture = @"
# Architecture

## Current Runtime Shape
- pending

## Service Model
- pending

## Data Flow
- pending

## Risks
- pending
"@

$memoryContextShort = @"
# Context Short

High-signal summary for constrained token windows.

- Project: $projectSlug
- Status: pending-analysis
- Top risks: pending
- Active priorities: pending
"@

$memoryContextFull = @"
# Context Full

Detailed long-form project context.

## Technical Fingerprint
- pending

## Architecture Decisions
- pending

## Known Issues
- pending
"@

$memoryKnownIssues = @"
# Known Issues

- pending
"@

$memoryDecisions = @"
# Technical Decisions

- pending
"@

$memoryRoadmap = @"
# Roadmap

## Current
- pending

## Next
- pending
"@

$taskDag = @"
# Task DAG

~~~yaml
tasks: []
~~~

## Scheduler Rule
- deny assignment when files_affected intersects with active entries in /ai-orchestrator/locks/locks.json
- deny assignment when dependencies are not done
"@

$taskBacklog = @"
# Backlog

## Pending
- none
"@

$taskInProgress = @"
# In Progress

## Active Tasks
- none
"@

$taskCompleted = @"
# Completed

## Finished Tasks
- none
"@

$taskExecutionHistory = @"
# Execution History

Chronological task execution history.
"@

$analysisSelfHealing = @"
# Self Healing

Tracks detected failures, generated repair tasks, and final resolutions.

## Active Incidents
- none
"@

$analysisArchitectureReport = @"
# Architecture Report

Generated architecture summary and improvement suggestions.
"@

$analysisDependencyGraph = @"
# Dependency Graph

Module and service dependency edges with impact analysis hints.
"@

$analysisCodeQuality = @"
# Code Quality

Static quality indicators: complexity, duplication, circular references, large files, and security signals.
"@

$projectPackReadme = @"
# Project Pack

Autocontained project pack manifest.
Contains project identity, docker ports, container map, and connection metadata.
"@

$projectPackManifest = @"
{
  "pack_version": 1,
  "project_name": "",
  "project_slug": "",
  "status": "pending-bootstrap",
  "generated_at": "$(Get-V2Timestamp)"
}
"@

$knowledgeBaseReadme = @"
# Knowledge Base

Cross-project and project-specific engineering learnings.
Use this store for repair summaries and reusable fix patterns.
"@

$knowledgeBaseLessonsReadme = @"
# Lessons Learned

Each resolved repair task should generate one lesson file:
- error signature
- root cause
- fix summary
- validation evidence
"@

$dockerReadme = @"
# Docker Layer

Generated assets:
- docker-compose.generated.yml
- app.Dockerfile.generated

Do not overwrite working project Docker files automatically.
"@

$dbConfig = @"
# Database Config

## Isolation
- Project namespace: $projectSlug
- Engine: unknown
- Confidence: unknown

## Connections
- app -> db: pending
- app -> cache: pending
"@

$schemaReadme = @"
# Database Schema Pointers

Store schema references and migration map for this project.
"@

$serviceReadme = @"
# Services

Service inventory and runtime boundaries.
"@

$experimentsReadme = @"
# Experiments

Track growth, feature, and performance experiments with outcomes.
"@

$experimentsFeatureTests = @"
# Feature Tests

- pending
"@

$experimentsUxTests = @"
# UX Tests

- pending
"@

$experimentsGrowthTests = @"
# Growth Tests

- pending
"@

$experimentsConversionTests = @"
# Conversion Tests

- pending
"@

$reportsReadme = @"
# Reports

Observer and orchestration incident reports are stored here.
"@

$dailyReport = @"
# Daily Report

Latest daily technical status snapshot.
"@

$weeklyReport = @"
# Weekly Report

Latest weekly technical status snapshot.
"@

$monthlyReport = @"
# Monthly Report

Latest monthly technical status snapshot.
"@

$communicationReadme = @"
# Communication

Cross-agent handoffs and decisions.
"@

$communicationMessages = @"
# Agent Messages

Cross-agent operational messages.
"@

$communicationDecisions = @"
# Decision Log

Technical and product decisions with rationale.
"@

$communicationHandoffs = @"
# Handoffs

Task handoff records between agents.
"@

$communicationWhiteboard = @"
# Whiteboard

Shared negotiation board for lock conflicts and cross-agent coordination.
"@

$communicationAlerts = @"
# Alerts

Conflict and risk alerts.
"@

$documentationArchitecture = @"
# Project Architecture Documentation

This file should always reflect the current validated architecture.
"@

$documentationDevGuide = @"
# Developer Guide

Operational commands, local setup, and quality gates.
"@

$documentationAgents = @"
# Agent Documentation

How each agent should act in this project.
"@

$documentationDecisions = @"
# Decisions

Record architecture and delivery decisions with evidence.
"@

$documentationApi = @"
# API Documentation

Generated API surface and contracts.
"@

$documentationDeploy = @"
# Deploy Guide

Environment, release, and rollback steps.
"@

$coreBootstrapReadme = @"
# Bootstrap Core

Mandatory bootstrap logic and validation controls.
"@

$coreProjectDetectorReadme = @"
# Project Detector

Detection and classification runtime for new/existing/legacy repositories.
"@

$coreDockerManagerReadme = @"
# Docker Manager

Container orchestration and lifecycle controls for project packs.
"@

$corePortValidatorReadme = @"
# Port Validator

Port inspection, collision detection, and remap policy.
"@

$coreConnectionManagerReadme = @"
# Connection Manager

Connection bootstrap, credential mapping, and runtime binding.
"@

$memoryNeo4jReadme = @"
# Neo4j Graph Memory

Structural memory artifacts and graph synchronization metadata.
"@

$memoryQdrantReadme = @"
# Qdrant Vector Memory

Semantic memory collections and embedding synchronization metadata.
"@

$memoryEmbeddingsReadme = @"
# Embeddings Layer

Embedding model metadata and ingestion checkpoints.
"@

$runtimeSchedulerReadme = @"
# Agent Scheduler Runtime

Agent assignment and workload balancing runtime artifacts.
"@

$runtimeDagReadme = @"
# DAG Engine Runtime

Task DAG execution runtime artifacts.
"@

$runtimeObserversReadme = @"
# Observers Runtime

Continuous monitoring and self-healing observer runtime artifacts.
"@

$projectsReadme = @"
# Projects Scope

Per-project isolated packs are created under:
- /ai-orchestrator/projects/<project_slug>/
"@

$locksObject = [PSCustomObject]@{
    locks = @()
}

$lockHistory = @"
# Lock History

Lock acquisition, release, expiration, and conflict events.
"@

$agentWorkload = [PSCustomObject]@{
    agents = @(
        [PSCustomObject]@{ name = "Codex"; role = "engineering"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
        [PSCustomObject]@{ name = "Claude"; role = "architecture"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
        [PSCustomObject]@{ name = "Antigravity"; role = "product"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
        [PSCustomObject]@{ name = "AI CTO"; role = "strategy"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
        [PSCustomObject]@{ name = "AI Architect"; role = "architecture"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
        [PSCustomObject]@{ name = "AI DevOps Engineer"; role = "devops"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
        [PSCustomObject]@{ name = "AI Security Engineer"; role = "security"; active_tasks = 0; completed_tasks = 0; last_assigned = "" },
        [PSCustomObject]@{ name = "AI Product Manager"; role = "product"; active_tasks = 0; completed_tasks = 0; last_assigned = "" }
    )
}

$projectStatePath = Join-Path $root "state/project-state.json"
if (-not (Test-Path -LiteralPath $projectStatePath) -or $Force) {
    $stateObject = [PSCustomObject]@{
        creation_date       = Get-V2Timestamp
        project_slug       = $projectSlug
        project_type       = "unknown"
        confidence         = "low"
        refactor_policy    = "unknown"
        status             = "pending-intake"
        project_pack_root  = ("ai-orchestrator/projects/{0}" -f $projectSlug)
        project_dna_path   = ("ai-orchestrator/projects/{0}/project_dna.json" -f $projectSlug)
        technical_fingerprint = [PSCustomObject]@{}
        verified_commands  = [PSCustomObject]@{
            install = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
            build   = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
            run     = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
            test    = [PSCustomObject]@{ value = "unknown"; confidence = "unknown" }
        }
        unknowns           = @()
        memory_mode        = "markdown-only"
        last_observer_run  = ""
        health_status      = "unknown"
        updated_at         = Get-V2Timestamp
    }
    Save-V2JsonContent -Path $projectStatePath -Value $stateObject
}
else {
    $stateObject = Get-V2JsonContent -Path $projectStatePath
    if ($stateObject) {
        $stateChanged = $false
        if (-not ($stateObject.PSObject.Properties.Name -contains "project_slug")) {
            Add-Member -InputObject $stateObject -MemberType NoteProperty -Name "project_slug" -Value $projectSlug -Force
            $stateChanged = $true
        }
        if (-not ($stateObject.PSObject.Properties.Name -contains "project_pack_root")) {
            Add-Member -InputObject $stateObject -MemberType NoteProperty -Name "project_pack_root" -Value ("ai-orchestrator/projects/{0}" -f $projectSlug) -Force
            $stateChanged = $true
        }
        if (-not ($stateObject.PSObject.Properties.Name -contains "project_dna_path")) {
            Add-Member -InputObject $stateObject -MemberType NoteProperty -Name "project_dna_path" -Value ("ai-orchestrator/projects/{0}/project_dna.json" -f $projectSlug) -Force
            $stateChanged = $true
        }
        if ($stateChanged) {
            Set-V2DynamicProperty -InputObject $stateObject -Name "updated_at" -Value (Get-V2Timestamp)
            Save-V2JsonContent -Path $projectStatePath -Value $stateObject
        }
    }
}

$projectDnaPath = Join-Path $root ("projects/{0}/project_dna.json" -f $projectSlug)
if (-not (Test-Path -LiteralPath $projectDnaPath) -or $Force) {
    $projectDna = [PSCustomObject]@{
        project_identity = [PSCustomObject]@{
            name = (Split-Path -Leaf $resolvedProjectPath)
            slug = $projectSlug
            created_at = Get-V2Timestamp
            updated_at = Get-V2Timestamp
            type = "unknown"
            status = "bootstrap"
            health_status = "unknown"
        }
        architecture = [PSCustomObject]@{
            style = "unknown"
            services = @()
            languages = @()
            databases = @()
        }
        service_map = [PSCustomObject]@{}
        patterns = [PSCustomObject]@{
            architecture_patterns = @()
            code_patterns = @()
        }
        tech_stack = [PSCustomObject]@{
            backend = "unknown"
            frontend = "unknown"
            database = "unknown"
            vector_memory = "qdrant"
            graph_memory = "neo4j"
        }
        evolution = [PSCustomObject]@{
            major_changes = @()
        }
        agent_knowledge = [PSCustomObject]@{
            known_hotspots = @()
            technical_debt = @()
        }
    }
    Save-V2JsonContent -Path $projectDnaPath -Value $projectDna
}

Write-V2File -Path (Join-Path $root "agents/registry.md") -Content $agentRegistry -Force:$Force
Write-V2File -Path (Join-Path $root "memory/world-model.md") -Content $memoryWorldModel -Force:$Force
Write-V2File -Path (Join-Path $root "memory/architecture.md") -Content $memoryArchitecture -Force:$Force
Write-V2File -Path (Join-Path $root "memory/context-short.md") -Content $memoryContextShort -Force:$Force
Write-V2File -Path (Join-Path $root "memory/context-full.md") -Content $memoryContextFull -Force:$Force
Write-V2File -Path (Join-Path $root "memory/known-issues.md") -Content $memoryKnownIssues -Force:$Force
Write-V2File -Path (Join-Path $root "memory/decisions.md") -Content $memoryDecisions -Force:$Force
Write-V2File -Path (Join-Path $root "memory/roadmap.md") -Content $memoryRoadmap -Force:$Force
Write-V2File -Path (Join-Path $root "tasks/task-dag.md") -Content $taskDag -Force:$Force
Write-V2File -Path (Join-Path $root "tasks/backlog.md") -Content $taskBacklog -Force:$Force
Write-V2File -Path (Join-Path $root "tasks/in-progress.md") -Content $taskInProgress -Force:$Force
Write-V2File -Path (Join-Path $root "tasks/completed.md") -Content $taskCompleted -Force:$Force
Write-V2File -Path (Join-Path $root "tasks/execution-history.md") -Content $taskExecutionHistory -Force:$Force
Write-V2File -Path (Join-Path $root "analysis/self-healing.md") -Content $analysisSelfHealing -Force:$Force
Write-V2File -Path (Join-Path $root "analysis/architecture-report.md") -Content $analysisArchitectureReport -Force:$Force
Write-V2File -Path (Join-Path $root "analysis/dependency-graph.md") -Content $analysisDependencyGraph -Force:$Force
Write-V2File -Path (Join-Path $root "analysis/code-quality.md") -Content $analysisCodeQuality -Force:$Force
Write-V2File -Path (Join-Path $root "project-pack/README.md") -Content $projectPackReadme -Force:$Force
Write-V2File -Path (Join-Path $root "project-pack/PACK_MANIFEST.json") -Content $projectPackManifest -Force:$Force
Write-V2File -Path (Join-Path $root "knowledge_base/README.md") -Content $knowledgeBaseReadme -Force:$Force
Write-V2File -Path (Join-Path $root "knowledge_base/lessons_learned/README.md") -Content $knowledgeBaseLessonsReadme -Force:$Force
Write-V2File -Path (Join-Path $root "docker/README.md") -Content $dockerReadme -Force:$Force
Write-V2File -Path (Join-Path $root "database/config.md") -Content $dbConfig -Force:$Force
Write-V2File -Path (Join-Path $root "database/schema/README.md") -Content $schemaReadme -Force:$Force
Write-V2File -Path (Join-Path $root "services/README.md") -Content $serviceReadme -Force:$Force
Write-V2File -Path (Join-Path $root "experiments/README.md") -Content $experimentsReadme -Force:$Force
Write-V2File -Path (Join-Path $root "experiments/feature-tests.md") -Content $experimentsFeatureTests -Force:$Force
Write-V2File -Path (Join-Path $root "experiments/ux-tests.md") -Content $experimentsUxTests -Force:$Force
Write-V2File -Path (Join-Path $root "experiments/growth-tests.md") -Content $experimentsGrowthTests -Force:$Force
Write-V2File -Path (Join-Path $root "experiments/conversion-tests.md") -Content $experimentsConversionTests -Force:$Force
Write-V2File -Path (Join-Path $root "reports/README.md") -Content $reportsReadme -Force:$Force
Write-V2File -Path (Join-Path $root "reports/daily-report.md") -Content $dailyReport -Force:$Force
Write-V2File -Path (Join-Path $root "reports/weekly-report.md") -Content $weeklyReport -Force:$Force
Write-V2File -Path (Join-Path $root "reports/monthly-report.md") -Content $monthlyReport -Force:$Force
Write-V2File -Path (Join-Path $root "communication/README.md") -Content $communicationReadme -Force:$Force
Write-V2File -Path (Join-Path $root "communication/messages.md") -Content $communicationMessages -Force:$Force
Write-V2File -Path (Join-Path $root "communication/decisions.md") -Content $communicationDecisions -Force:$Force
Write-V2File -Path (Join-Path $root "communication/handoffs.md") -Content $communicationHandoffs -Force:$Force
Write-V2File -Path (Join-Path $root "communication/whiteboard.md") -Content $communicationWhiteboard -Force:$Force
Write-V2File -Path (Join-Path $root "communication/alerts.md") -Content $communicationAlerts -Force:$Force
Write-V2File -Path (Join-Path $root "documentation/architecture.md") -Content $documentationArchitecture -Force:$Force
Write-V2File -Path (Join-Path $root "documentation/dev-guide.md") -Content $documentationDevGuide -Force:$Force
Write-V2File -Path (Join-Path $root "documentation/agents.md") -Content $documentationAgents -Force:$Force
Write-V2File -Path (Join-Path $root "documentation/decisions.md") -Content $documentationDecisions -Force:$Force
Write-V2File -Path (Join-Path $root "documentation/api.md") -Content $documentationApi -Force:$Force
Write-V2File -Path (Join-Path $root "documentation/deploy-guide.md") -Content $documentationDeploy -Force:$Force
Write-V2File -Path (Join-Path $root "core/bootstrap/README.md") -Content $coreBootstrapReadme -Force:$Force
Write-V2File -Path (Join-Path $root "core/project_detector/README.md") -Content $coreProjectDetectorReadme -Force:$Force
Write-V2File -Path (Join-Path $root "core/docker_manager/README.md") -Content $coreDockerManagerReadme -Force:$Force
Write-V2File -Path (Join-Path $root "core/port_validator/README.md") -Content $corePortValidatorReadme -Force:$Force
Write-V2File -Path (Join-Path $root "core/connection_manager/README.md") -Content $coreConnectionManagerReadme -Force:$Force
Write-V2File -Path (Join-Path $root "memory/neo4j_graph/README.md") -Content $memoryNeo4jReadme -Force:$Force
Write-V2File -Path (Join-Path $root "memory/qdrant_vectors/README.md") -Content $memoryQdrantReadme -Force:$Force
Write-V2File -Path (Join-Path $root "memory/embeddings/README.md") -Content $memoryEmbeddingsReadme -Force:$Force
Write-V2File -Path (Join-Path $root "runtime/agent_scheduler/README.md") -Content $runtimeSchedulerReadme -Force:$Force
Write-V2File -Path (Join-Path $root "runtime/dag_engine/README.md") -Content $runtimeDagReadme -Force:$Force
Write-V2File -Path (Join-Path $root "runtime/observers/README.md") -Content $runtimeObserversReadme -Force:$Force
Write-V2File -Path (Join-Path $root "projects/README.md") -Content $projectsReadme -Force:$Force

$locksPath = Join-Path $root "locks/locks.json"
if (-not (Test-Path -LiteralPath $locksPath) -or $Force) {
    Save-V2JsonContent -Path $locksPath -Value $locksObject
}
Write-V2File -Path (Join-Path $root "locks/lock-history.md") -Content $lockHistory -Force:$Force

$workloadPath = Join-Path $root "agents/workload.json"
if (-not (Test-Path -LiteralPath $workloadPath) -or $Force) {
    Save-V2JsonContent -Path $workloadPath -Value $agentWorkload
}

Write-Output "Initialized AI orchestration layer at $root"

