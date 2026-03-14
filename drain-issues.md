---
allowed-tools: Bash(git:*), Bash(gh:*), Task
argument-hint: [label:filter] [--max-parallel=N] [--dry-run] [--get-all] [--plan-review]
description: Autonomous issue processor - analyzes dependencies, batches independent issues, repeats until done. Use --plan-review for Codex plan refinement before implementation.
---

# Autonomous Issue Drainer

## Context

Repository:
!`git remote get-url origin`

Current branch:
!`git branch --show-current`

---

## Input Parsing

Arguments: **$ARGUMENTS**

| Option | Default | Description |
|--------|---------|-------------|
| `label:<name>` | (none) | Only process issues with this label |
| `--max-parallel=N` | 2 | Max concurrent subagents (keep low to avoid context overflow) |
| `--dry-run` | false | Analyze only, don't process |
| `--no-merge` | false | Review PRs but don't auto-merge |
| `--skip-review` | false | Create PRs without review/merge |
| `--full-review` | false | Use Claude↔Codex review loop instead of basic review. Codex reviews each PR, Claude fixes issues, repeat until approved (max 15 iterations per PR). Much more thorough but slower (~5-15 min per PR). |
| `--plan-review` | false | Use Codex to review the implementation plan before coding begins. Each subagent writes a plan, Codex reviews it (max 3 rounds), then implements the refined plan. Catches design issues early. |
| `--get-all` | false | Process all open issues regardless of who is assigned. Without this flag, issues already assigned to someone else are skipped. |

---

## CRITICAL: Context Management

**Problem:** Multiple parallel agents returning results can overflow context before auto-compact triggers.

**Solution:** This command uses a staged approach:

1. **Small batches:** Default max-parallel is 2, max 3
2. **Minimal agent output:** Agents return only structured JSON, not verbose logs
3. **Wave isolation:** Each wave keeps results minimal. Context auto-compacts as needed.
4. **Temp file persistence:** Wave results are saved to temp files so they survive compaction

---

## Phase 1: Fetch All Open Issues

```bash
# Get all open issues with full details (including assignees)
gh issue list --state open --json number,title,body,labels,assignees --limit 50
```

If a label filter was provided:
```bash
gh issue list --state open --label "<label>" --json number,title,body,labels,assignees --limit 50
```

### Assignment Filtering

After fetching, get your own GitHub username:
```bash
gh api user --jq '.login'
```

Then filter the issue list:

- **Default behavior:** Skip any issue where `assignees` is non-empty AND none of the assignees is you. These belong to someone else — don't touch them.
- **`--get-all` mode:** Include all issues regardless of assignment. Issues assigned to others are still claimed (you'll be added as assignee in Phase 4).
- **Unassigned issues** (empty `assignees`): always included.
- **Issues already assigned to you:** always included.

Log any skipped issues clearly:
```
Skipped (assigned to others):
  #17 - Refactor auth middleware  [assigned: alice]
  #23 - Fix payment bug           [assigned: bob, carol]
```

---

## Phase 2: Dependency Analysis

For each issue, analyze for dependencies:

### Dependency Indicators

1. **Blocking references (creates dependency):** "depends on #X", "blocked by #X", "after #X", "requires #X", "follow-up to #X", "Step N of..."
2. **Tracking references (NOT a dependency):** Umbrella/epic issues that list sub-issues (e.g. "Sub-tasks: #12, #15, #22" or "This epic tracks: #12, #15"). These are **trackers**, not blockers — the sub-issues are independent and can run in parallel.
3. **Shared files:** Issues that likely touch the same files (based on description)
4. **Feature dependencies:** Issue B needs a feature that Issue A introduces

### CRITICAL: Umbrella/Epic Detection

An issue is an **umbrella/epic** if:
- Its title contains words like "Epic", "Umbrella", "Tracking", "Meta"
- Its body uses checklist format listing multiple issues: `- [ ] #12`, `- [x] #15`
- It describes itself as tracking progress across multiple issues

**Umbrella issues do NOT block their sub-issues.** Sub-issues are independent. Place sub-issues in Wave 1 (or their appropriate wave based on their own dependencies), and the umbrella issue either:
- Gets processed in parallel with the sub-issues (if it has its own implementation work), or
- Is skipped/deferred if it has no independent work beyond tracking

### Analysis Process

```
For each issue:
  1. Check if it's an umbrella/epic — if so, mark it and do NOT treat its referenced issues as dependents
  2. Read issue body for BLOCKING dependency keywords (depends on, blocked by, after #X, requires #X)
  3. Extract mentioned files/components
  4. Build dependency graph using only blocking relationships
```

### Dependency Graph Output

```
Issue Dependency Analysis:
━━━━━━━━━━━━━━━━━━━━━━━━━

Independent (can run in parallel):
  #12 - Fix login timeout
  #15 - Add dark mode toggle
  #22 - Update docs

Dependent chains:
  #18 → #24 (18 must complete first)
  #31 → #32 → #35 (sequential chain)

Unclear (need manual review):
  #28 - May conflict with #12 (both touch auth/)
```

---

## Phase 3: Wave Planning

Group independent issues into waves:

```
Wave 1: [#12, #15, #22, #41] - 4 independent issues (including sub-issues of any umbrella)
Wave 2: [#18, #28, #33] - 3 issues (after wave 1 deps resolve)
Wave 3: [#24, #32] - 2 issues (depend on wave 2)
Wave 4: [#35] - 1 issue (depends on wave 3)

Umbrella/Epic issues: [#10 - Epic: Dashboard] → placed in Wave 1 alongside sub-issues (or skipped if tracking-only)

Total: 4 waves to process 10 issues
```

**Umbrella placement rule:** Umbrella issues with their own implementation work go into Wave 1 as independent. Umbrella issues that are purely tracking (no code to write) can be skipped — they'll auto-close when sub-issues are linked and closed.

**Present the wave plan to the user and wait for confirmation before proceeding.**

---

## Phase 4: Process Current Wave

### Step 4.0: Claim Issues (Self-Assign)

Before launching any subagents, assign yourself to every issue in this wave:

```bash
# Assign yourself to all issues in the wave before starting work
for issue in <wave-issue-numbers>; do
  gh issue edit $issue --add-assignee @me
done
```

This marks the issues as in-progress so other contributors (or other `/drain-issues` sessions) don't pick them up simultaneously.

### Step 4.1: Launch Subagents

For each issue in the current wave, launch parallel subagents:

```
Launch N parallel Task agents (respecting --max-parallel):

Each agent receives:
"Process issue #XX end-to-end:
- Detect default branch: git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo 'main'
- Create worktree ../fix-XX-<short-desc> from origin/<default-branch>
- Fetch the full issue including all comments: `gh issue view XX --json number,title,body,labels,comments`
- Read the issue body AND all comments — comments often contain reproduction steps, clarifications, or constraints that are critical to the correct solution
- Identify ROOT CAUSE (not surface-level symptoms — no z-index hacks, no retry loops without understanding why)
- Write a failing test that reproduces the bug
- Create a brief implementation plan (root cause, proposed solution, files to modify, risks & edge cases, testing strategy)
  **If `--plan-review` was requested:** Run Plan Review Loop before implementing (see below)
- Implement the fix/feature with minimal changes
- Run tests (new test should pass, full suite should pass)
- Update CHANGELOG.md if one exists (add entry under [Unreleased])
- Stage specific files (never use git add -A)
- Create atomic commit (NO Co-Authored-By)
- Create PR linked to issue (NO Claude attribution)
- Self-review the changes

PLAN REVIEW LOOP (only if --plan-review was requested):
After writing your plan, before writing any code, run this loop (max 3 rounds: 1 original + 2 refinements):

For each round:
  PLAN_REVIEW_PROMPT='You are a senior engineer reviewing an implementation plan. Be critical and concise.

ISSUE:
<issue title and body>

PROPOSED PLAN (round N):
<the plan>

Review for:
1. Incorrect or incomplete root cause diagnosis
2. Missing edge cases not addressed
3. Files or components that should be modified but are not listed
4. Overly complex approach when a simpler one exists
5. Missing steps (migrations, cache invalidation, config changes, etc.)

Respond with:
- APPROVED — if the plan is solid and ready to implement
- [ISSUE] <description> — for each problem found (be specific)

Do not repeat issues already addressed in prior rounds.'

  Run: codex exec \"$PLAN_REVIEW_PROMPT\" --full-auto --ephemeral --json 2>/dev/null
  Parse the last agent_message from JSONL output.

  If APPROVED and no [ISSUE] items → exit loop, proceed to implement.
  If [ISSUE] items found → revise plan, increment round, repeat.
  If round reaches 3 → proceed with current best plan regardless.

After the loop, implement using the final refined plan.

IMPORTANT - Return ONLY this minimal JSON (no other text):
{\"issue\": XX, \"pr\": <number|null>, \"status\": \"success|failed\", \"error\": \"<short error if failed>\"}"
```

### Process in Sub-Batches (Context Safety)

If wave has 4+ issues, split into sub-batches of 2:

```
Wave 1 has 6 issues: [#12, #15, #22, #41, #28, #33]

Sub-batch 1: Process #12, #15 in parallel
  → Collect minimal results

Sub-batch 2: Process #22, #41 in parallel
  → Collect minimal results

Sub-batch 3: Process #28, #33 in parallel
  → Collect minimal results
```

---

## Phase 5: Wave Results

After wave completes, aggregate PR creation results:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Wave 1 - PR Creation Results
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

| Issue | Title              | PR   | Status |
|-------|--------------------|------|--------|
| #12   | Fix login timeout  | #45  | ✅     |
| #15   | Add dark mode      | #46  | ✅     |
| #22   | Update docs        | #47  | ✅     |
| #41   | Refactor utils     | -    | ❌     |

PRs Created: 3/4
Failed: #41 - Test failures in utils.test.ts

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Phase 6: Review & Auto-Merge (SEQUENTIAL)

**CRITICAL: Review and merge each PR one at a time. Do NOT batch reviews.**

### Review Mode Selection

There are two review modes. Select based on the `--full-review` flag:

- **Default (basic review):** Uses `/review-changes` — Claude reviews the diff for breaking changes, regressions, missing changelog, etc. Fast (~1-2 min per PR).
- **`--full-review` mode:** Uses the Claude↔Codex review loop — Codex reviews the PR, Claude fixes any [P1]/[P2] issues, repeat until Codex approves (max 15 iterations). Much more thorough but slower (~5-15 min per PR). Codex receives iteration history so it won't re-raise dismissed issues.

### Review Loop

For each PR created in this wave, do this loop:

```
for each PR in [#45, #46, #47]:

  1. REVIEW the PR:

     If --full-review mode:
       Run the full Claude↔Codex review loop for this PR:

       a. Get the PR number
       b. Execute the /full-review workflow inline:
          - Get PR info and checkout the branch
          - Initialize iteration history
          - Run Codex review with `codex exec` (including iteration history context on rounds 2+)
          - Use Codex's default model only; do not pass `--model` or `-c model=...`
          - Parse review for [P1]/[P2] issues
          - Fix issues, commit, push
          - Update iteration history with outcomes (FIXED/DISMISSED)
          - Repeat until Codex approves or 15 iterations
       c. Record the final result (approved / max iterations reached)

       NOTE: The /full-review loop handles its own fix-commit-push cycle.
       After the loop completes, the PR is either clean (Codex approved) or
       has been iterated to convergence.

     If basic review mode (default):
       Run /review-changes
       This checks: changelog, debug code, secrets, breaking changes, regressions

  2. IMMEDIATELY after review:

     If APPROVED (basic review passed, or Codex approved in full-review):
       ```bash
       # Leave a COMMENT review (can't self-approve on GitHub)
       gh pr review <PR> --comment --body "Review passed: no breaking changes, no debug code, tests pass, root cause addressed"
       gh pr merge <PR> --rebase --delete-branch
       ```
       → Log: "PR #XX merged successfully"

     If REJECTED (basic review found issues, or Codex hit max iterations with unresolved [P1]s):
       ```bash
       gh pr review <PR> --comment --body "<issue found - needs fix before merge>"
       ```
       → Log: "PR #XX needs attention: <reason>"
       → Create GitHub issue for non-blocking findings: `gh issue create --title "[Review] <finding>" --body "Found during review of PR #XX"`

  3. Move to next PR
```

### Example Execution

```
PRs to review: [#45, #46, #47]

─── PR #45 ───
Run: review changes via git diff
Result: ✅ SAFE TO MERGE - no breaking changes, changelog present
Action: gh pr merge 45 --squash --delete-branch
Result: ✅ PR #45 merged

─── PR #46 ───
Run: review changes via git diff
Result: ❌ NEEDS CHANGES - Missing changelog entry
Action: gh pr comment 46 --body "Needs CHANGELOG entry before merge"
Result: ⚠️ PR #46 queued for manual fix

─── PR #47 ───
Run: review changes via git diff
Result: ✅ SAFE TO MERGE - no issues found
Action: gh pr merge 47 --squash --delete-branch
Result: ✅ PR #47 merged
```

### Merge Command (Copy-Paste Ready)

```bash
# NOTE: Cannot self-approve PRs on GitHub, so skip approval and merge directly
# If admin/merge without approval is enabled:
gh pr merge <NUMBER> --rebase --delete-branch

# If repo requires approval, add a comment instead:
gh pr comment <NUMBER> --body "Self-review passed: changelog present, no debug code, no secrets"
gh pr merge <NUMBER> --rebase --delete-branch --admin
```

### Handling Self-Authored PRs

**Problem:** GitHub won't let you approve your own PR.

**Solutions:**

1. **If you have admin rights:** Use `--admin` flag to bypass approval requirement
   ```bash
   gh pr merge <NUMBER> --rebase --delete-branch --admin
   ```

2. **If repo allows merge without approval:** Just merge directly
   ```bash
   gh pr merge <NUMBER> --rebase --delete-branch
   ```

3. **If approval is required:** Leave comment and skip merge (manual step needed)
   ```bash
   gh pr comment <NUMBER> --body "Automated review passed - ready for human approval"
   ```
   → Log PR as "awaiting human approval"

### After All PRs in Wave Reviewed

Track results as you go:

```
Wave 1 Review Summary:
━━━━━━━━━━━━━━━━━━━━━━

✅ PR #45 (Issue #12) - merged
✅ PR #47 (Issue #22) - merged
⚠️ PR #46 (Issue #15) - needs changelog

Merged: 2
Needs attention: 1
```

### Clean Up Merged Worktrees

After each successful merge:

```bash
# Clean up the worktree for merged PR
git worktree remove ../fix-<issue>-* 2>/dev/null || true
git worktree prune
```

---

## Phase 7: Wave Summary & Decision

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Wave 1 Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Issues processed:  4
PRs created:       3
PRs reviewed:      3
Auto-merged:       2
Needs attention:   1

Issues closed:     2 (#12, #15)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Decision Logic

- **All merged:** Proceed to next wave
- **Some need attention:**
  - Log them for manual review later
  - Continue with next wave (don't block independent work)
- **Critical failure (>50% failed review):** Pause and ask for guidance

---

## Phase 8: Context Clear & Continue

**IMPORTANT:** Keep agent outputs minimal (JSON only) to prevent context overflow. Context auto-compacts when needed.

### After Wave Completes

1. Save wave results to a temp file (for reference across waves):
   ```bash
   echo '{"wave": 1, "merged": [45,46], "failed": [47]}' > /tmp/drain-wave-1.json
   ```

2. Check remaining issues:
   ```bash
   gh issue list --state open --json number,title --limit 50
   ```

4. If issues remain → Continue to next wave

### Loop Continuation Message

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Wave 1 Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Merged: #45, #46
Failed: #47 (needs changelog)
Remaining issues: 6

Continuing to Wave 2...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Phase 9: Final Summary (When Complete)

When no issues remain:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
All Issues Drained!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Total waves:           4
Total issues:          10
PRs created:           9
PRs auto-merged:       7
PRs need attention:    2

Issues closed:         7
Issues still open:     3 (failed/blocked)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRs Needing Manual Review:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PR #47 (Issue #22) - Missing changelog
PR #52 (Issue #31) - Test failures

View all: gh pr list --author "@me" --state open

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Failed Issues (Need Investigation):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

#41 - Could not create PR (test failures)
#35 - Blocked by dependency

View: gh issue list --state open

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Cleanup:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

git worktree list | grep fix- | awk '{print $1}' | xargs -I {} git worktree remove {}
git worktree prune
git fetch --prune

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Dry Run Mode

If `--dry-run` is specified, only perform analysis:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Dry Run Analysis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Open issues: 10

Dependency analysis:
- Independent: 4 issues
- Dependent chains: 2 chains (6 issues)

Estimated waves: 4
Wave breakdown:
  Wave 1: #12, #15, #22, #41
  Wave 2: #18, #28, #33
  Wave 3: #24, #32
  Wave 4: #35

No changes made (dry run).
Run without --dry-run to process.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Safety Features

1. **Max 3 parallel agents** (default 2) - Prevent system overload and context overflow
2. **Confirmation before each wave** - User can abort
3. **Automatic pause on high failure rate** - >50% failures stops processing
4. **Worktree isolation** - Each issue in separate worktree
5. **No force operations** - Safe git operations only

---

## Usage Examples

```bash
# Process all open issues in waves (full automation)
/drain-issues

# Only process bugs
/drain-issues label:bug

# Analyze dependencies without processing
/drain-issues --dry-run

# Limit parallelism
/drain-issues --max-parallel=2

# Review PRs but merge manually
/drain-issues --no-merge

# Just create PRs, skip review (faster but less safe)
/drain-issues --skip-review

# Combine options
/drain-issues label:enhancement --max-parallel=3 --dry-run

# Full automation for bugs only
/drain-issues label:bug --max-parallel=4

# Codex reviews plans before implementation (catches design issues early)
/drain-issues --plan-review

# Plan review + full review (maximum quality: review plan, then review code)
/drain-issues --plan-review --full-review

# Thorough review with Claude↔Codex loop (slower but catches more)
/drain-issues --full-review

# Full automation with Codex review for critical bugs
/drain-issues label:bug --full-review

# Codex review without auto-merge (review only)
/drain-issues --full-review --no-merge

# Plan review for complex features only
/drain-issues label:feature --plan-review
```
