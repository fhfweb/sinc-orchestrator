# Universal Intake V2

- Generated At: 2026-03-12T06:13:32
- Project: project0
- Type: existing
- Confidence: high
- Status: blocked-waiting-answers
- Refactor Policy: unknown
- Fingerprint: 656b13c23bb028843212b36879d1fff0ef35b22a396f12848ec60616e0b5043d

## Commands
| Action | Value | Confidence |
|---|---|---|
| install | unknown | unknown |
| build | unknown | unknown |
| run | powershell -ExecutionPolicy Bypass -File .\orchestrator.ps1 | inferred |
| test | Invoke-Pester | inferred |

## Technical Fingerprint
- Primary Language: powershell
- Languages: powershell:65, python:27
- Frameworks: 
- Frontend Frameworks: 
- Architecture Pattern: monolith
- Legacy Signal Score: 0
- Service Structure: scripts
- API Patterns: 
- Database: unknown (unknown)
- Build Systems: Python packaging
- Package Managers: pip
- CI/CD: 
- Tests: 

## Dependency Graph
- Detection Mode: semantic
- Detection Reason: semantic-selected
- Heuristic Edge Count: 0
- Semantic Edge Count: 0
- Modules: 3
- Edges: 0
- Circular Dependency Detected: False
- Sample Edges: none
- Semantic Enabled: True
- Semantic Engine: semantic-v1
- Semantic Files Scanned: 80
- Semantic Parse Errors: 0
- Semantic Adapters Used: powershell, python

## Code Quality
- Code Files: 15
- Complexity Proxy: 262 (high)
- Large Files: 11
- Duplicate Groups: 0
- Dead Code Candidates: 0
- Vulnerability Signals: 0

## Risks
- No CI/CD workflow detected.
- No Docker assets detected.
- Transactional database engine is unknown.
- Large file concentration suggests high complexity hotspots.

## Unknowns
- database-engine

## Open Questions
- Which transactional database engine should this project use?