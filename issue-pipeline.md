---
allowed-tools: Bash(git:*), Bash(gh:*), Bash(grep:*), Bash(find:*), Bash(cat:*), Bash(npm:*), Bash(cargo:*), Bash(pnpm:*), Task
argument-hint: <issue-description OR issue-number> [--full-review] [--plan-review]
description: Full pipeline: create issue (if needed), fix it, create PR, and self-review. Use --full-review for Claude↔Codex review loop. Use --plan-review for Claude↔Codex plan refinement loop before implementation.
---

# Issue Pipeline - Automated Flow

## Context

Current repository:
!`git remote get-url origin`

Current branch:
!`git branch --show-current`

Recent issues:
!`gh issue list --limit 5`

---

## Workflow Mode Detection

Based on input: **$ARGUMENTS**

Determine the mode:
- If input is a **number only** (e.g., "42") → Fix existing issue #42
- If input is **text description** → Create new issue first, then fix it

---

## Mode A: Fix Existing Issue

If input is an issue number:

1. **Skip to fixing** - Issue already exists
2. Proceed directly to the Fix Phase below

---

## Mode B: Create + Fix New Issue

If input is a description:

### Quick Issue Creation (Streamlined)

Create issue directly with sensible defaults:

```bash
# Determine type from description keywords
# - Contains "bug", "broken", "error", "fail" → bug
# - Contains "add", "new", "implement" → feature
# - Otherwise → enhancement

gh issue create \
  --title "[Type] $ARGUMENTS" \
  --body "## Description

$ARGUMENTS

## Acceptance Criteria

- [ ] Implementation complete
- [ ] Tests pass
- [ ] No regressions

---
"
```

**Capture the issue number from output for next phase.**

---

## Prompt Enhancement Phase

Before launching the fix agent, sharpen the problem statement. A vague input produces a vague plan. This step uses Codex to analyze the issue + codebase and produce a precise, implementation-ready problem statement.

```bash
ISSUE_CONTEXT=$(gh issue view <number> --json number,title,body,labels,comments --jq '
  "Issue #" + (.number|tostring) + ": " + .title + "\n\n" + .body +
  if (.comments | length) > 0 then
    "\n\nComments:\n" + (.comments | map("- " + .body) | join("\n"))
  else "" end
')

ENHANCE_PROMPT="You are a technical analyst. Read this GitHub issue and produce a sharpened problem statement for an implementation agent.

ISSUE:
$ISSUE_CONTEXT

Output a precise problem statement that includes:
1. Root cause hypothesis (what is actually broken, not just symptoms)
2. Specific files/areas most likely involved (based on the description)
3. Edge cases to handle
4. Success criteria (how to know the fix is correct)
5. What tools the agent should use (tests to run, how to compile, etc.)

Be specific and technical. This will be passed directly to a coding agent."

ENHANCED_PROBLEM=$(codex exec "$ENHANCE_PROMPT" --full-auto --ephemeral --json 2>/dev/null | python3 -c "
import json, sys
result = None
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        event = json.loads(line)
        if event.get('type') == 'item.completed':
            item = event.get('item', {})
            if item.get('type') == 'agent_message' and item.get('text'):
                result = item['text']
    except: pass
print(result or '')
")
```

If Codex is unavailable or returns empty, skip this phase and use the raw issue description.

---

## Fix Phase (Using Subagent)

Launch a dedicated agent to handle the fix in isolation:

```
Use the Task tool to spawn a subagent with the following prompt:

"Fix GitHub issue #<number> in this repository.

ENHANCED PROBLEM STATEMENT (pre-analyzed — use this as your primary guide):
<insert $ENHANCED_PROBLEM here if available, otherwise omit this section>

Instructions:
1. Detect default branch: git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo 'main'
2. Create a git worktree from origin/<default-branch>
3. Fetch the full issue including all comments: `gh issue view <number> --json number,title,body,labels,comments`
   Read the issue body AND all comments — comments often contain reproduction steps, clarifications, or constraints that are critical to the correct solution.
4. Investigate the codebase to find ROOT CAUSE — do NOT apply surface-level fixes (z-index hacks, retry loops, overflow-hidden). Ask 'why does this happen?' until you reach the actual cause.
5. Write a failing test that reproduces the bug
6. Create a brief implementation plan (account for ALL available tools: Bash to run tests/compile/lint, Read/Grep/Glob to explore code, Edit/Write to modify files). Structure it as:
   - Root cause
   - Proposed solution (high-level)
   - Files to modify
   - Risks & edge cases
   - Testing strategy
   **If `--plan-review` was requested:** after writing the plan, run the Plan Review Loop before implementing anything (see Plan Review Loop section below).
   **Otherwise:** proceed directly to step 7.
7. Implement the fix with minimal changes
8. Run tests — the new test should pass, and the full test suite should pass (npm test, cargo test, or equivalent)
9. Run the linter/type checker (npm run lint, cargo clippy, etc.)
10. Update CHANGELOG.md at the REPO ROOT if one exists (add entry under [Unreleased]). Check for correct file path first.
11. Stage specific files (never use git add -A)
12. Create a commit with conventional format: fix|feat(scope): description - Fixes #<number>
   DO NOT include any Co-Authored-By lines or Claude/Anthropic attribution
13. Push the branch and create a PR using gh pr create
   DO NOT include any 'Generated by Claude' or similar attribution in PR body
   Verify any labels exist before using them: gh label list --json name --jq '.[].name'
14. Return the PR number when done

PLAN REVIEW LOOP (only if --plan-review was requested):
After writing your plan in step 6, before writing any code, run this loop (max 3 rounds total: 1 original plan + 2 refinements):

For each round:
  PLAN_REVIEW_PROMPT="You are a senior engineer reviewing an implementation plan. Be critical and concise.

ISSUE:
$ISSUE_CONTEXT

PROPOSED PLAN (round $ROUND):
$IMPLEMENTATION_PLAN

Review the plan for:
1. Incorrect or incomplete root cause diagnosis
2. Missing edge cases not addressed by the plan
3. Files or components that should be modified but aren't listed
4. Overly complex approach when a simpler one exists
5. Missing steps (migrations, cache invalidation, config changes, etc.)

Respond with:
- APPROVED — if the plan is solid and ready to implement
- [ISSUE] <description> — for each problem found (be specific)

Do not repeat issues that were already addressed in prior rounds."

  Run: codex exec "$PLAN_REVIEW_PROMPT" --full-auto --ephemeral --json 2>/dev/null
  Parse the last agent_message from JSONL output.

  If response contains APPROVED and no [ISSUE] items → exit loop, proceed to implement.
  If response contains [ISSUE] items → revise your plan to address each one, increment ROUND, repeat.
  If ROUND reaches 3 → proceed to implement with the current (best) plan regardless.

After the loop, implement using the final refined plan.

GUARDRAILS:
- NEVER push directly to master/main
- NEVER add redirects or middleware unless explicitly requested
- Identify ROOT CAUSE before implementing (no surface-level fixes)
- Run full test suite AND linter before committing

Work autonomously. Make reasonable decisions. Only ask if truly blocked."
```

**Wait for PR number from subagent.**

---

## Review Phase

### Review Mode Selection

There are two review modes. Select based on the `--full-review` flag in `$ARGUMENTS`:

- **Default (basic review):** Claude reviews the PR diff for breaking changes, regressions, debug code, secrets, etc. Fast (~1-2 min).
- **`--full-review` mode:** Uses the Claude↔Codex review loop — Codex reviews the PR, Claude fixes any [P1]/[P2] issues, commits and pushes, repeat until Codex approves (max 15 iterations). Codex receives iteration history so it won't re-raise dismissed issues. Much more thorough but slower (~5-15 min).

### If basic review (default):

1. Get the full PR diff: `gh pr diff <pr-number>`
2. Check for:
   - Code changes match the issue requirements
   - No breaking changes (function signatures, API contracts, exports)
   - No debug code (console.log, print statements)
   - No hardcoded secrets or credentials
   - Tests are included/passing
   - Changelog updated (if project has CHANGELOG.md)
   - No regressions in related functionality

Based on the review:
- If **SAFE TO MERGE** → Proceed to merge
- If **NEEDS CHANGES** → List required fixes and stop

### If `--full-review` mode:

Run the full Claude↔Codex review loop for this PR inline:

1. Get PR info: `gh pr view <pr-number> --json title,body,headRefName,baseRefName,files`
2. Checkout the PR branch: `gh pr checkout <pr-number>`
3. Determine base branch: `gh pr view <pr-number> --json baseRefName -q '.baseRefName'`
4. Initialize `ITERATION_HISTORY = ""`
5. **Loop (max 15 iterations):**
   a. Build Codex prompt:
      - Iteration 1: `review`
      - Iteration N>1: Include `ITERATION_HISTORY` with instructions to skip dismissed/fixed issues
   b. Run Codex with the default model only: `codex exec "$REVIEW_PROMPT" --base "origin/$BASE_BRANCH" --title "..." --full-auto --ephemeral --json 2>/dev/null`
      Do not pass `--model` or `-c model=...`.
   c. Parse JSONL output for the last `agent_message` text
   d. If no [P1]/[P2] issues → **APPROVED**, break
   e. Fix [P1]/[P2] issues, commit, push
   f. Update `ITERATION_HISTORY` with outcomes (FIXED/DISMISSED/NOTED)
   g. Repeat

Based on the result:
- If **Codex approved** → Proceed to improvement passes
- If **Max iterations with unresolved [P1]s** → List remaining issues and stop

---

## Improvement Passes (Post-Implementation)

After the review passes (basic or full-review), run up to **2 improvement passes** on the finished implementation. This is different from the review loop — instead of finding bugs, Codex generates a fresh prompt focused on improving the *quality* of the existing output.

**Why this works:** The review loop catches correctness issues. Improvement passes catch quality issues (better abstractions, clearer naming, edge cases the original prompt didn't anticipate). Empirically: 1-2 passes improves quality; beyond 2 passes, quality degrades.

**Run passes in a fresh context window (new Codex invocation each time):**

For each pass (max 2, stop early if no improvements found):

```bash
PASS_N=$(($PASS_N + 1))  # starts at 1

PR_DIFF=$(gh pr diff <pr-number>)

IMPROVE_PROMPT="You reviewed and the implementation is functionally correct. Now write a new prompt that, if sent to a coding agent with fresh context, would produce a higher-quality version of this implementation.

ORIGINAL ISSUE:
$ISSUE_CONTEXT

CURRENT IMPLEMENTATION DIFF:
$PR_DIFF

Write a prompt that:
1. Describes what was implemented (so the agent has context)
2. Identifies specific quality improvements to make (naming, structure, edge cases, test coverage, error messages)
3. Lists exact files and functions to improve
4. Specifies the success bar ('the implementation should...')

Be concrete. Don't suggest rewrites — suggest targeted improvements to the existing code."

IMPROVEMENT_PROMPT=$(codex exec "$IMPROVE_PROMPT" --full-auto --ephemeral --json 2>/dev/null | python3 -c "
import json, sys
result = None
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        event = json.loads(line)
        if event.get('type') == 'item.completed':
            item = event.get('item', {})
            if item.get('type') == 'agent_message' and item.get('text'):
                result = item['text']
    except: pass
print(result or '')
")
```

If `$IMPROVEMENT_PROMPT` is empty or says "no improvements" → **stop early, don't run pass 2.**

Otherwise, apply the improvements: checkout the PR branch, read the relevant files, apply the suggested improvements, run tests, commit (`improve: quality pass N`), push.

---

## Final Output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Pipeline Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Issue:  #<number> - <title>
PR:     #<pr-number>
Status: <review result>
URL:    <pr-url>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Error Handling

If any phase fails:
1. Report which phase failed
2. Provide the error details
3. Suggest manual intervention steps
4. Do NOT proceed to next phase

**Common failure scenarios:**
- **Issue creation fails:** Check `gh auth status` and repo permissions
- **Issue number not parsed:** Extract from `gh issue create` output, which prints the URL
- **Subagent fails to create worktree:** Check if directory already exists, or if branch name conflicts
- **Tests fail:** Report which tests failed and the error output, suggest manual investigation
- **PR creation fails:** Check if branch was pushed, if remote is accessible

---

## Notes

- This command uses subagents to maintain context isolation
- Each phase runs with fresh context (no /clear needed)
- Worktrees ensure parallel work doesn't conflict
- Review happens automatically but human can override
