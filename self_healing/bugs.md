# Bug Registry

All bugs discovered by QA, agents, or users are tracked here.
Part of the Self-Healing Development Loop.

---

## Bug Status Flow

```
open -> assigned -> fix-in-progress -> fix-deployed -> verified -> closed
open -> won't-fix (with justification)
```

---

## Severity Levels

| Severity | Definition |
|----------|-----------|
| Critical | System down, data loss, security breach |
| High | Major feature broken, significant UX degradation |
| Medium | Feature partially broken, workaround exists |
| Low | Minor UX issue, edge case |

---

## Active Bugs

*(No active bugs)*

---

## Bug Entry Template

```markdown
### BUG-[ID]: [Title]
- **Severity:**    [Critical / High / Medium / Low]
- **Status:**      open
- **Found By:**    [QA Agent / Codex / User / Monitoring]
- **Found At:**    [ISO timestamp]
- **Assigned To:** [agent or unassigned]
- **Task Created:** [TASK-ID in TASK_BOARD.md]

#### Description
[What the bug is — precise and observable]

#### Steps to Reproduce
1. [Step 1]
2. [Step 2]
3. Observed: [what happens]
4. Expected: [what should happen]

#### Impact
[Who is affected and how severely]

#### Affected Files
- [file path]

#### Root Cause (filled after investigation)
[Why the bug exists]

#### Fix Applied (filled after fix)
[What was changed to fix it]

#### Verified By (filled after QA confirms fix)
[Agent name and timestamp]
```

---

## Closed Bugs

*(No closed bugs yet)*
