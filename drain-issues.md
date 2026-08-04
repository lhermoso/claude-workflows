---
allowed-tools: Bash(git:*), Bash(gh:*), Task
argument-hint: [label:filter] [--max-parallel=N] [--dry-run] [--get-all] [--no-plan-review] [--basic-review] [--no-verify]
description: Autonomous issue processor - analyzes dependencies, batches independent issues, repeats until done. ALL quality gates ON by default: two-pass Codex plan refinement before coding, plan-adherence verification (inline drift report) after coding, and the Claude↔Codex full-review loop before merge. Opt out with --no-plan-review / --no-verify / --basic-review.
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
| `--skip-review` | false | Skip Phase 6 review/merge entirely (PRs left open) |
| `--basic-review` | false | Use fast basic diff review (~1-2 min per PR) instead of the DEFAULT Claude↔Codex review loop (Codex reviews each PR, Claude fixes issues, repeat until approved, max 15 iterations per PR, ~5-15 min). |
| `--no-plan-review` | false | Skip the DEFAULT two-pass Claude↔Codex plan refinement before coding. Pass A: Codex verifies diagnosis against real code (max 2 rounds). Pass B: Codex traces fix's side-effects through the codebase (max 3 rounds). Plan + review history written to `.pair/` (gitignored, local to the worktree — never committed). Catches fix-breaks-something-else bugs that diagnosis-only review misses. |
| `--no-verify` | false | Skip the DEFAULT Phase 5.5 plan-adherence verification: an inline pass checks each plan claim against the PR diff, reverse-traces unplanned changes, and posts an Implementation Report on each PR. PLAN_NOT_MET PRs are blocked from auto-merge. |
| `--get-all` | false | Process all open issues regardless of who is assigned. Without this flag, issues already assigned to someone else are skipped. |

The legacy `--plan-review` / `--full-review` flags are accepted but redundant — they are now the default behavior. Fastest escape hatch (roughly the old default): `--no-plan-review --no-verify --basic-review`.

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
- Create implementation plan in `.pair/PLAN.md` at worktree root. Use these EXACT sections — Codex review depends on this structure:

  ```markdown
  ## Diagnosis
  - Root cause (mechanism, not symptom)
  - Confirmed by: <file>:<line> references for each claim
  - Observability traps (states that look healthy but aren't)

  ## Proposed Fix
  - High-level approach
  - Why this and not <alternative-1>, <alternative-2>

  ## Files & Line Numbers
  - <path>:<line> — what changes, why

  ## Side-Effects Trace
  - For each modified function: who else calls it, what assumptions break
  - For each new code path: what existing tests cover it, what doesn't
  - For each shared mutable state touched: concurrency / dedup / cache invariants

  ## Acceptance Criteria
  - [ ] specific, measurable checkboxes

  ## Test Plan
  - Failing test to write first (path + assertion)
  - Regression surface (which existing tests must still pass)

  ## What I Am Most Likely Wrong About
  - One paragraph naming the weakest assumption. Codex reviews this paragraph FIRST.
  ```

  The `Side-Effects Trace` section is non-negotiable — it catches "the fix breaks something else" bugs that diagnosis-only review misses.

  **Plan review runs by DEFAULT:** Run Plan Review Loop before implementing (see below). Skip only if `--no-plan-review` was passed.
- Implement the fix/feature with minimal changes
- Run tests (new test should pass, full suite should pass)
- Update CHANGELOG.md if one exists (add entry under [Unreleased])
- Stage specific files (never use git add -A)
- Create atomic commit (NO Co-Authored-By)
- Create PR linked to issue (NO Claude attribution)
- Self-review the changes

PLAN REVIEW LOOP (default — skipped only if --no-plan-review was passed):

Goal: replicate the Claude↔Codex pair-review pattern that produces sharpened plans (see polymarkets-weather#231 for reference). Plan is reviewed against the **actual code in the worktree**, not just plan-shape correctness.

Run TWO distinct passes in sequence. Merging them dilutes both.

**Bookkeeping (applies to both passes):**
- All Codex invocations run from worktree root so codex reads real files.
- Append every Codex output to `.pair/REVIEW.md` with a `## Round N — <pass-name>` header. Pass the file in subsequent prompts so Codex doesn't re-raise dismissed issues.
- Always pipe prompts via stdin (large prompts as positional args silently hang per CLAUDE.md).
- Capture stderr — empty stdout ≠ approval. Retry once on empty output. If second attempt also empty, log warning and proceed without plan-review for this issue.
- Use: `printf '%s' "$PROMPT" | codex exec - -s read-only --ephemeral --json` (read-only sandbox signals review intent, faster). `codex exec` is non-interactive and auto-approves within the sandbox — no `-a`/`--ask-for-approval` (that's a global flag and errors after `exec`) and no `--full-auto` (legacy alias; errors on `review`). The `codex exec review` subcommand takes neither `-s` nor `-a`.

---

**Pass A — Diagnosis verification (max 2 rounds)**

Verify root cause is real before discussing any fix.

  DIAG_PROMPT='You are reviewing the DIAGNOSIS section of an implementation plan against actual code in this repo.

ISSUE:
<issue title + body + comments>

PLAN:
<contents of .pair/PLAN.md>

YOUR TASK — diagnosis only, ignore the proposed fix:
1. Read the "What I Am Most Likely Wrong About" paragraph first. Take it seriously.
2. For EVERY <file>:<line> reference in the Diagnosis section, read the file and verify the claim.
3. Identify symptoms misread as root cause.
4. Identify observability traps the plan missed.

OUTPUT FORMAT (markdown):
## Diagnosis — Confirmed
- <claim> — verified at <file>:<line>
## Diagnosis — Corrections
- [WRONG] <claim> — actual mechanism is <X> at <file>:<line>
## Diagnosis — Missing
- <observability trap or co-existing failure> at <file>:<line>
## Verdict
DIAGNOSIS_CONFIRMED | DIAGNOSIS_NEEDS_REVISION

Cite line numbers. If no issues after honest search, say so.'

  Pipe via stdin (same mechanics as before — PROMPT_FILE / OUT_FILE / ERR_FILE / retry-on-empty).
  Append output to `.pair/REVIEW.md`.

  If `DIAGNOSIS_NEEDS_REVISION` → revise Diagnosis section of `.pair/PLAN.md`, re-run Pass A.
  If `DIAGNOSIS_CONFIRMED` → proceed to Pass B.

---

**Pass B — Fix-impact trace (max 3 rounds)**

Diagnosis confirmed. Now check the proposed fix doesn't break something else.

  FIX_PROMPT='Diagnosis was confirmed. Now review the PROPOSED FIX against the actual code.

ISSUE:
<issue title + body + comments>

PLAN:
<contents of .pair/PLAN.md>

PRIOR REVIEW ROUNDS (do not re-raise issues already addressed or dismissed):
<contents of .pair/REVIEW.md>

YOUR TASK:
1. For every function the plan modifies, find all call sites. What assumptions break for callers not listed in the plan?
2. For every new code path, identify shared mutable state, dedup keys, cache entries, locks. Find asymmetries (set-membership check on read but not on write, or vice versa).
3. For every helper the plan reuses, check whether that helper has guards/early-returns/retro-windows that would still block the fix.
4. For every new code path, identify which existing tests cover it and which do not.
5. Find at least one of: incorrect line reference, side-effect not in plan, helper/guard that still blocks the fix, dedup/cache asymmetry, missing lock around shared mutable state, lifecycle issue (detached task without supervision). If after honest search you find none, say so.

OUTPUT FORMAT (markdown):
## Fix — Confirmed
- <element of fix> — traced, no side-effects found
## Fix — Bugs Introduced
- [BUG] <description> — at <file>:<line> — why it breaks: <X> — proposed correction: <one line>
## Fix — Missing From Plan
- <missing step / invariant / test> — at <file>:<line>
## Sharper Alternative (optional)
- 3-5 bullets if a materially simpler/safer approach exists, otherwise omit.
## Verdict
FIX_APPROVED | FIX_NEEDS_REVISION

Cite line numbers. No vague "consider edge cases".'

  Same stdin/retry/parse mechanics. Append to `.pair/REVIEW.md`.

  If `FIX_APPROVED` and no [BUG] items → exit loop, proceed to implement.
  If [BUG] items → revise Side-Effects Trace + Files sections of `.pair/PLAN.md`, increment round, repeat.
  If round reaches 3 → proceed with current best plan, but log unresolved [BUG] items in the PR body under `## Unresolved Codex concerns`.

---

After both passes, implement using the final `.pair/PLAN.md`. Do NOT commit `.pair/` — it is gitignored working-notes scratch and must stay local to the worktree. Never `git add -f` it. If the pair-session reasoning is worth preserving in the PR record, mirror a concise summary into the PR body instead.

IMPORTANT - Do NOT remove your worktree when done — Phase 5.5 verification reads `.pair/PLAN.md` from it (gitignored, exists only there). Cleanup happens in the main loop after merge.

IMPORTANT - Return ONLY this minimal JSON (no other text):
{\"issue\": XX, \"pr\": <number|null>, \"worktree\": \"<abs path>\", \"status\": \"success|failed\", \"error\": \"<short error if failed>\"}"
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

## Phase 5.5: Plan-Adherence Verification (DEFAULT, runs in MAIN LOOP)

Skip only if `--no-verify` was passed.

This phase asks a different question than review: not "is the code good?" but **"is the code what the plan said we'd build?"** It runs once per wave, **inline in the main conversation** — no subagents, no fan-out — after PRs are created (Phase 5) and BEFORE review/merge (Phase 6).

**Cost rule: budget ONE `gh pr diff` per PR plus targeted file reads.** Do not spawn agents or workflows here, and do not re-review code quality — that is Phase 6's job.

**CRITICAL: do not remove any worktree before this phase completes** — verifiers read `.pair/PLAN.md` from the worktrees (gitignored, exists only there).

### Step 1 — Build the contract list (inline)

For each successful PR in the wave (from the subagent JSON: issue, pr, worktree):
1. Read `<worktree>/.pair/PLAN.md`.
2. Parse it into discrete claims: Acceptance Criteria checkboxes (type `ac`), Files & Line Numbers entries (`file`), Test Plan items (`test`), Side-Effects Trace invariants (`side-effect`). Assign ids like `ac1`, `f1`, `t1`, `s1`.
3. **Fallback:** if PLAN.md is missing or its ACs are generic boilerplate, use the issue's own acceptance criteria as the contract and note it in that PR's report header: `> Verified against issue acceptance criteria — no implementation plan existed.`

### Step 2 — Verify each PR inline, one at a time

For each PR in the wave:

1. Read its diff once: `gh pr diff <pr>`. That single read is the evidence base for every claim on that PR.
2. Walk the claim list and classify each claim:
   - **MATCHED** — implemented as the plan stated
   - **DIVERGED** — implemented, but differently than planned; note exactly how
   - **MISSING** — not implemented at all
   - **UNVERIFIABLE** — cannot be determined from the code
3. Cite `file:line` evidence. Only open a worktree file when the diff alone can't settle a claim — read the specific region, not the whole file.
4. Before marking a claim DIVERGED or MISSING, re-check the diff for the change under a different name or location. A rename or a move is not a miss.
5. Reverse-trace in the same pass: note any hunk that maps to no claim — those are the unplanned changes. Collapse mechanical noise (imports, formatting, lockfiles) into one line.

Then move to the next PR. Nothing here runs in parallel, and nothing spawns an agent.

### Step 3 — Per-PR Implementation Report

Post one report per PR (`gh pr comment <pr> --body "<report>"`):

```markdown
## Implementation Report — PR #<pr> (Issue #<n>)

| # | Plan item | Status | Evidence |
|---|-----------|--------|----------|
| ac1 | <claim text> | ✅ as planned | src/retry.ts:42 |
| f2  | <claim text> | 🔀 diverged | guard moved to middleware.ts:30 — <how/why> |
| t1  | <claim text> | ❌ missing | no test reproduces the bug |

**Unplanned changes (in diff, not in plan):**
- utils.ts:88 — refactored `parseConfig` — <description>

**Summary:** N as planned · N diverged · N missing · N unplanned
```

Status mapping: `MATCHED` → ✅ · `DIVERGED` → 🔀 · `MISSING` → ❌ · `UNVERIFIABLE` → ⚠️.

### Step 4 — Record per-PR verdict (gates Phase 6)

- **PLAN_MET:** no ❌ on any `ac` or `test` claim.
- **PLAN_NOT_MET:** at least one ❌ on an `ac` or `test` claim. Attempt ONE fix cycle: apply the missing piece in the worktree, commit, push, re-verify **only the failed claims**. If still failing → the PR is **blocked from auto-merge** in Phase 6; leave it open with the report comment as the record and move on.

```
Wave 1 Verification Summary:
━━━━━━━━━━━━━━━━━━━━━━━━━━━
PR #45 (Issue #12): PLAN_MET      (5 ✅ · 1 🔀 · 0 ❌ · 1 ➕)
PR #46 (Issue #15): PLAN_MET      (4 ✅ · 0 🔀 · 0 ❌ · 0 ➕)
PR #47 (Issue #22): PLAN_NOT_MET  (3 ✅ · 1 🔀 · 1 ❌ · 2 ➕) → blocked from auto-merge
```

Carry each PR's 🔀 and ➕ items into its Phase 6 review prompt — they are the first things the reviewer should scrutinize.

---

## Phase 6: Review & Auto-Merge (SEQUENTIAL)

**CRITICAL: Review and merge each PR one at a time. Do NOT batch reviews.**

### Review Mode Selection

There are two review modes. **Full review is the DEFAULT.** Use basic only if `--basic-review` was passed:

- **Default (full review):** Uses the Claude↔Codex review loop — Codex reviews the PR, Claude fixes any [P1]/[P2] issues, repeat until Codex approves (max 15 iterations). Thorough (~5-15 min per PR). Codex receives iteration history so it won't re-raise dismissed issues.
- **`--basic-review` mode:** Uses `/review-changes` — Claude reviews the diff for breaking changes, regressions, missing changelog, etc. Fast (~1-2 min per PR).

**Verification gate (from Phase 5.5):** a PR marked PLAN_NOT_MET is NEVER auto-merged, regardless of review outcome — review it anyway (the findings are still useful), but leave it open with a comment pointing at the Implementation Report. Include each PR's 🔀 diverged and ➕ unplanned items in the review prompt.

### Review Loop

For each PR created in this wave, do this loop:

```
for each PR in [#45, #46, #47]:

  1. REVIEW the PR:

     If full review (default):
       Run the full Claude↔Codex review loop for this PR:

       a. Get the PR number
       b. Execute the /full-review workflow inline:
          - Get PR info and checkout the branch
          - Initialize iteration history
          - Run Codex review via `codex exec review - --ephemeral --json` with the prompt piped on stdin (including iteration history context on rounds 2+); do NOT use `--full-auto`
          - Use Codex's default model only; do not pass `--model` or `-c model=...`
          - Parse review for [P1]/[P2] issues
          - Fix issues, commit, push
          - Update iteration history with outcomes (FIXED/DISMISSED)
          - Repeat until Codex approves or 15 iterations
       c. Record the final result (approved / max iterations reached)

       NOTE: The /full-review loop handles its own fix-commit-push cycle.
       After the loop completes, the PR is either clean (Codex approved) or
       has been iterated to convergence.

     If --basic-review mode:
       Run /review-changes
       This checks: changelog, debug code, secrets, breaking changes, regressions

  2. IMMEDIATELY after review:

     If the PR was marked PLAN_NOT_MET in Phase 5.5:
       Do NOT merge, even if the review approved.
       ```bash
       gh pr comment <PR> --body "Review passed but plan-adherence verification found unmet acceptance criteria — see the Implementation Report above. Holding for manual decision."
       ```
       → Log: "PR #XX held: plan not met" and move to next PR

     If APPROVED (Codex approved in full review, or basic review passed) AND PLAN_MET:
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
# Process all open issues with ALL quality gates (plan review, verification report, full review)
/drain-issues

# Only process bugs (all gates still on)
/drain-issues label:bug

# Analyze dependencies without processing
/drain-issues --dry-run

# Limit parallelism
/drain-issues --max-parallel=2

# All gates, but merge manually
/drain-issues --no-merge

# Fast mode — roughly the old default behavior (no plan review, no report, basic diff review)
/drain-issues --no-plan-review --no-verify --basic-review

# Fastest possible: PRs only, no gates at all
/drain-issues --skip-review --no-plan-review --no-verify

# Keep the Implementation Report but skip the slow Codex review loop
/drain-issues --basic-review

# Skip pre-coding plan review but still verify implementation against the plan
/drain-issues --no-plan-review

# Trust the report, skip post-hoc verification only
/drain-issues --no-verify
```
