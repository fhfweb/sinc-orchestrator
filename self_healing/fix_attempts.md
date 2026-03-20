# Fix Attempt Log

Records every attempt to fix a bug, including failed attempts.
Part of the Self-Healing Development Loop.

---

## Purpose

- Prevents agents from repeating the same failed fix strategy
- Builds a knowledge base of what does and does not work
- Helps escalate bugs that resist multiple fix attempts

---

## Escalation Rule

If a bug has 3+ failed fix attempts:
1. Mark bug as ESCALATE in bugs.md
2. Write a detailed analysis entry here
3. Create a new task assigned to a different agent
4. Notify via AGENT_HANDOFF.md

---

## Fix Attempt Format

```markdown
### FIX-[ID] for BUG-[ID]
- **Agent:**     [agent name]
- **Attempt #:** [number]
- **Date:**      [ISO timestamp]
- **Status:**    [succeeded / failed / partial]

#### Approach
[What the agent tried to fix the bug]

#### Files Changed
- [file path] — [what was changed]

#### Result
[What happened after the fix was applied]

#### Why It Failed (if applicable)
[Analysis of why this approach did not work]

#### Next Suggested Approach
[If failed, what the next agent should try instead]
```

---

## Active Fix Attempts

*(No active fix attempts)*
