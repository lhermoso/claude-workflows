---
allowed-tools: Bash, Read, Edit, Write, Grep, Glob, Agent
argument-hint: <pr-number>
description: Claude ↔ Codex review loop - Codex reviews, Claude fixes, repeat until approved or max iterations
---

# Full Review: Claude ↔ Codex Review Loop

You are entering an automated review-fix loop for **PR #$ARGUMENTS**.

## Rules

- **MAX_ITERATIONS = 15** — stop after 15 rounds regardless
- Each iteration: Codex reviews → if changes requested → you fix & push → Codex reviews again
- You may ONLY modify files that are part of this PR's diff
- After fixing, commit and push to the PR branch (do NOT create a new PR)
- When approved, print a summary and stop

## Phase 0: Setup

1. Get PR info and checkout the branch:

```bash
gh pr view $ARGUMENTS --json title,body,headRefName,baseRefName,files
```

2. Checkout the PR branch:
```bash
gh pr checkout $ARGUMENTS
```

3. Determine the base branch:
```bash
BASE_BRANCH=$(gh pr view $ARGUMENTS --json baseRefName -q '.baseRefName')
echo "Base branch: origin/$BASE_BRANCH"
```

4. Initialize the **iteration history log** — this is a running record you maintain in memory throughout the loop. Start empty:

```
ITERATION_HISTORY = ""
```

## Phase 1: Run Codex Review

Build the Codex review prompt. On the **first iteration**, the prompt is simply `review`. On subsequent iterations, include the iteration history so Codex has context about prior rounds and doesn't re-raise dismissed issues.

**First iteration prompt:**
```
review
```

**Subsequent iteration prompt (iteration N > 1):**
```
Review this PR.

CONTEXT FROM PREVIOUS ITERATIONS:
${ITERATION_HISTORY}

INSTRUCTIONS:
- Do NOT re-raise issues that were explicitly dismissed or marked as out-of-scope above.
- Do NOT re-raise issues that were already fixed in a previous iteration.
- Focus only on NEW problems or verify that previously requested fixes were applied correctly.
- If a previously raised issue was fixed incorrectly, you may re-raise it with details on what's still wrong.
```

Run the Codex review using `codex exec` with the constructed prompt and parse the output:

```bash
codex exec "$REVIEW_PROMPT" --base "origin/$BASE_BRANCH" --title "$(gh pr view $ARGUMENTS --json title -q '.title')" --full-auto --ephemeral --json 2>/dev/null
```

**IMPORTANT: Parsing the output.**
The output is JSONL (one JSON object per line). The review text is in the LAST event where:
- `type` == `"item.completed"`
- `item.type` == `"agent_message"`
- The review text is in `item.text`

Use this Python script to extract the review:

```bash
codex exec "$REVIEW_PROMPT" --base "origin/$BASE_BRANCH" --title "$(gh pr view $ARGUMENTS --json title -q '.title')" --full-auto --ephemeral --json 2>/dev/null | python3 -c "
import json, sys
review = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
        if event.get('type') == 'item.completed':
            item = event.get('item', {})
            if item.get('type') == 'agent_message' and item.get('text'):
                review = item['text']
    except:
        pass
if review:
    print(review)
else:
    print('NO_REVIEW_OUTPUT')
"
```

This command may take 3-10 minutes. That is normal — Codex is doing a thorough review with file access.

## Phase 2: Parse the Review

After extracting the review text:

1. **If the review contains `[P1]` or `[P2]` tags** → changes are requested, proceed to Phase 3
2. **If the review says "no issues", "looks good", "LGTM", or has only `[P3]` tags** → APPROVED, skip to Phase 4
3. **If `NO_REVIEW_OUTPUT`** → treat as approved (Codex had no concerns)

Print the full review text so the user can see what Codex found.

## Phase 3: Fix Issues

For each `[P1]` and `[P2]` issue:

1. Read the file mentioned in the review
2. Understand the issue Codex described
3. Fix it properly (don't just suppress — address the root cause)
4. If the fix requires changes to other files (tests, config), make those too

After all fixes:

```bash
git add -A
git commit -m "fix: address Codex review feedback (iteration N)

$(echo "Fixed issues:")
$(echo "- [P1] description...")
$(echo "- [P2] description...")

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
git push
```

### Update Iteration History

After each iteration (whether you fixed issues or dismissed them), append a summary to `ITERATION_HISTORY`. This gets passed to Codex in the next round so it has full context.

For each issue Codex raised, record ONE of these outcomes:

```
Iteration N:
- [P1] <issue description> → FIXED: <what you changed>
- [P2] <issue description> → FIXED: <what you changed>
- [P1] <issue description> → DISMISSED: <your reasoning>
- [P2] <issue description> → DISMISSED: <your reasoning>
- [P3] <issue description> → NOTED (minor, not blocking)
```

Example:
```
Iteration 3:
- [P1] SQL injection in user_logs query → FIXED: parameterized the query in services.py:45
- [P1] Migration ordering — app will fail if deployed before migration 0007 → DISMISSED: Standard Django deployment runs migrations before restarting servers. Not fixable without restructuring migration strategy, out of PR scope.
- [P2] Missing index on user_id column → FIXED: added index in migration 0007
- [P3] Consider adding type hints to helper functions → NOTED (minor, not blocking)
```

Then **go back to Phase 1** with the next iteration number.

## Phase 4: Approved — Summary

When Codex approves (or max iterations reached), print:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ PR #$ARGUMENTS — Codex Review Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iterations: N
Result: APPROVED / MAX_ITERATIONS_REACHED

Review History:
- Iteration 1: [summary]
- Iteration 2: [summary]
...

Fixes Applied:
- [list of commits pushed]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Important Notes

- The Codex review runs in `--full-auto` mode with workspace-write sandbox — it can read all project files for context
- Codex uses `[P1]` (critical), `[P2]` (major), `[P3]` (minor) severity tags
- Only `[P1]` and `[P2]` block approval — `[P3]` are suggestions
- The iteration history is passed to Codex each round, so it knows what was fixed and what was dismissed. This should prevent the same issue from being re-raised. If Codex STILL re-raises a dismissed issue despite the history, skip it — do not fix or re-dismiss, just move on.
- Always preserve the PR's original intent — don't refactor unrelated code
