---
allowed-tools: Bash(git:*), Bash(gh:*), Task
argument-hint: [label:filter] [--max-parallel=N] [--dry-run] [--get-all] [--no-plan-review] [--basic-review] [--no-verify] [--plan-model=M] [--code-model=M] [--review-model=M]
description: Autonomous issue processor - analyzes dependencies, batches independent issues, repeats until done. Plans and reviews run on claude-fable-5 (fallback opus), code is written by claude-opus-5. ALL quality gates ON by default: a single Codex plan review before coding, plan-adherence verification after coding, and the Claude↔Codex full-review loop before merge. Opt out with --no-plan-review / --no-verify / --basic-review.
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
| `--no-plan-review` | false | Skip the DEFAULT single Codex plan review before coding. Claude writes the plan, Codex reviews it once against real code (diagnosis + fix side-effects), Claude absorbs the findings into the plan, then coding starts — no re-review round. Plan + review written to `.pair/` (gitignored, local to the worktree — never committed). |
| `--no-verify` | false | Skip the DEFAULT Phase 5.5 plan-adherence verification: an inline pass checks each plan claim against the PR diff, reverse-traces unplanned changes, and posts an Implementation Report on each PR. PLAN_NOT_MET PRs are blocked from auto-merge. |
| `--get-all` | false | Process all open issues regardless of who is assigned. Without this flag, issues already assigned to someone else are skipped. |
| `--plan-model=M` | `fable` | Override the planner model (`fable\|opus\|sonnet\|haiku`). |
| `--code-model=M` | `opus` | Override the coder model. |
| `--review-model=M` | `fable` | Override the reviewer model. |

The legacy `--plan-review` / `--full-review` flags are accepted but redundant — they are now the default behavior. Fastest escape hatch (roughly the old default): `--no-plan-review --no-verify --basic-review`.

---

## Model Routing (applies to every Claude-side agent in this command)

| Role | Covers | Model | Fallback |
|------|--------|-------|----------|
| **Planner** | root-cause investigation, writing and revising `.pair/PLAN.md`, running the single Codex plan review and absorbing its findings | `fable` (claude-fable-5) | `opus` |
| **Reviewer** | plan-adherence verification + Implementation Report, basic diff review, triaging Codex `[P1]`/`[P2]` findings, merge decisions | `fable` (claude-fable-5) | `opus` |
| **Coder** | failing test, implementation, lint/test runs, CHANGELOG, commits, PR creation, applying fixes the reviewer decided to accept | `opus` (claude-opus-5) | — |

Rules:

1. **Every `Task` launch passes an explicit `model`.** Never inherit the session model — the routing above is the contract.
2. **Fable fallback:** if a launch with `model: fable` fails because the model is unavailable/invalid/over quota, retry the *identical* launch once with `model: opus`, and log `⚠️ fable unavailable — <role> downgraded to opus`. Never downgrade a planner or reviewer to a smaller model (`sonnet`/`haiku`): planning and judging stay on fable or opus.
3. **Coder agents never plan.** A coder receives a finished `.pair/PLAN.md` and implements it. If the coder believes the plan is wrong, it stops and returns `"status": "plan_rejected"` with the reason — the planner (fable) revises, then the coder resumes. Coders do not silently redesign.
4. **Reviewers never write code.** A reviewer produces verdicts and a fix list; the fixes are applied by a coder agent (opus).
5. **Codex is unchanged** — it stays the external adversarial reviewer on its own default model. Never pass `--model` / `-c model=...` to Codex.
6. The dependency/wave analysis in Phases 2–3 is planning work; when it is non-trivial (>10 issues or ambiguous chains), delegate it to a planner agent (`fable`) instead of doing it in the main loop.

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

### Step 4.1: Launch Planner Agents (model: `fable`, fallback `opus`)

For each issue in the current wave, launch a **planner** Task agent — parallel, respecting `--max-parallel`. Planners investigate and write the plan; they do **not** implement.

```
Launch N parallel Task agents with model: "fable" (on failure retry once with model: "opus"):

Each planner receives:
"Plan the fix for issue #XX. You are the PLANNER — do not implement anything, do not write production code, do not commit. Your deliverable is a reviewed plan.

- Detect default branch: git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo 'main'
- Create worktree ../fix-XX-<short-desc> from origin/<default-branch>
- Fetch the full issue including all comments: `gh issue view XX --json number,title,body,labels,comments`
- Read the issue body AND all comments — comments often contain reproduction steps, clarifications, or constraints that are critical to the correct solution
- Identify ROOT CAUSE (not surface-level symptoms — no z-index hacks, no retry loops without understanding why)
- Specify the failing test that reproduces the bug (path + assertion) — the coder writes it, you specify it
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

  **Plan review runs by DEFAULT:** Run the single-pass Plan Review below before handing off. Skip only if `--no-plan-review` was passed.

- Write a CONTEXT BRIEF to `.pair/CONTEXT.md` at worktree root. This is the handoff that stops the coder from re-reading everything you just read. Without it, every file you opened gets opened again from a cold context — the single largest source of duplicated tokens in this pipeline. Include:

  ```markdown
  ## Files Read (and what matters in each)
  - <path> — <what it does; the specific region that matters>: lines <a>-<b>

  ## Key Symbols
  - <symbol> — <path>:<line> — real signature, and who calls it

  ## Entry Points
  - Where execution starts for the affected path, in call order

  ## Conventions Observed
  - Test framework, how tests are named and located here
  - Error handling / logging / config idioms this repo actually uses

  ## Commands
  - Run tests: <exact command> · Run one test: <exact command>
  - Lint/typecheck: <exact command> · Build (if needed): <exact command>

  ## Dead Ends
  - Files or approaches investigated and ruled out — so the coder doesn't repeat the search

  ## Not Yet Read
  - Relevant things you did NOT open, so the coder knows the brief's edges
  ```

  Write it for an agent with ZERO prior context on this repo. Quote real signatures and line numbers instead of describing them — a vague brief just moves the re-exploration downstream and buys nothing.

IMPORTANT — Do NOT implement, do NOT commit, do NOT open a PR. A separate coder agent does that from your plan.
IMPORTANT — Do NOT remove the worktree. The coder and the verifier both need it.

Return ONLY this minimal JSON (no other text):
{\"issue\": XX, \"worktree\": \"<abs path>\", \"plan\": \"<abs path to .pair/PLAN.md>\", \"context_brief\": \"<abs path to .pair/CONTEXT.md>\", \"plan_review\": \"reviewed|skipped|unavailable\", \"unresolved\": [\"<short [BUG] item>\"], \"status\": \"success|failed\", \"error\": \"<short error if failed>\"}

PLAN REVIEW (default — skipped only if --no-plan-review was passed):

Goal: ONE adversarial Codex pass over the finished plan, checked against the **actual code in the worktree** — not just plan-shape correctness. Codex reviews once, you absorb the findings into `.pair/PLAN.md`, then the coder starts. There is NO re-review round: the revised plan is final.

**Bookkeeping:**
- Run Codex from the worktree root so it reads real files.
- Write the Codex output to `.pair/REVIEW.md` under a `## Codex Plan Review` header.
- Always pipe the prompt via stdin (large prompts as positional args silently hang per CLAUDE.md).
- Capture stderr — empty stdout ≠ approval. Retry ONCE on empty output. If the second attempt is also empty, log a warning, set `plan_review: "unavailable"`, and hand the unreviewed plan to the coder.
- Use: `printf '%s' "$PROMPT" | codex exec - -s read-only --ephemeral --json 2> "$CODEX_ERR"` (read-only sandbox signals review intent, faster). `codex exec` is non-interactive and auto-approves within the sandbox — no `-a`/`--ask-for-approval` (that's a global flag and errors after `exec`) and no `--full-auto` (legacy alias; errors on `review`).

  REVIEW_PROMPT='You are reviewing an implementation plan against the actual code in this repo. Cover the diagnosis AND the proposed fix in this single pass — there will be no second round.

ISSUE:
<issue title + body + comments>

PLAN:
<contents of .pair/PLAN.md>

YOUR TASK:

A. DIAGNOSIS
1. Read the "What I Am Most Likely Wrong About" paragraph FIRST. Take it seriously.
2. For EVERY <file>:<line> reference in the Diagnosis section, read the file and verify the claim.
3. Identify symptoms misread as root cause.
4. Identify observability traps the plan missed (states that look healthy but aren'"'"'t).

B. FIX IMPACT
5. For every function the plan modifies, find all call sites. What assumptions break for callers not listed in the plan?
6. For every new code path, identify shared mutable state, dedup keys, cache entries, locks. Find asymmetries (set-membership check on read but not on write, or vice versa).
7. For every helper the plan reuses, check whether that helper has guards/early-returns/retro-windows that would still block the fix.
8. For every new code path, identify which existing tests cover it and which do not.
9. Find at least one of: incorrect line reference, side-effect not in plan, helper/guard that still blocks the fix, dedup/cache asymmetry, missing lock around shared mutable state, lifecycle issue (detached task without supervision). If after honest search you find none, say so.

OUTPUT FORMAT (markdown):
## Diagnosis — Confirmed
- <claim> — verified at <file>:<line>
## Diagnosis — Corrections
- [WRONG] <claim> — actual mechanism is <X> at <file>:<line>
## Diagnosis — Missing
- <observability trap or co-existing failure> at <file>:<line>
## Fix — Confirmed
- <element of fix> — traced, no side-effects found
## Fix — Bugs Introduced
- [BUG] <description> — at <file>:<line> — why it breaks: <X> — proposed correction: <one line>
## Fix — Missing From Plan
- <missing step / invariant / test> — at <file>:<line>
## Sharper Alternative (optional)
- 3-5 bullets if a materially simpler/safer approach exists, otherwise omit.

Cite line numbers. No vague "consider edge cases".'

---

**Absorb the review — you do this yourself, with NO second Codex call:**

- Every `[WRONG]` item → rewrite the Diagnosis section of `.pair/PLAN.md`.
- Every `[BUG]` item → rewrite the Side-Effects Trace and Files & Line Numbers sections.
- Every `Fix — Missing From Plan` item → add that step / invariant / test to the plan.
- A `Sharper Alternative` you accept → replace Proposed Fix, and record why under "Why this and not <alternative>".
- Anything you deliberately reject → append a one-line reason to `.pair/REVIEW.md` under `## Dismissed`, and list it in `unresolved[]` so the coder records it in the PR body under `## Unresolved Codex concerns`.

Do NOT re-run Codex on the revised plan. Set `plan_review: "reviewed"` and hand off.

---

After the review is absorbed, the plan is final. Do NOT commit `.pair/` — it is gitignored working-notes scratch and must stay local to the worktree. Never `git add -f` it. If the pair-session reasoning is worth preserving in the PR record, mirror a concise summary into the PR body instead. `.pair/CONTEXT.md` follows the same rule: local to the worktree, never committed, and never deleted before Phase 5.5 has run. Then return the planner JSON above."
```

### Step 4.2: Launch Coder Agents (model: `opus`)

For each issue whose planner returned `"status": "success"`, launch a **coder** Task agent with `model: "opus"`. Coders run in the planner's worktree and implement the approved plan — nothing more.

```
Launch N parallel Task agents with model: "opus" (respecting --max-parallel):

Each coder receives:
"Implement issue #XX from an already-reviewed plan. You are the CODER — the plan is the contract. Do not re-plan, do not redesign.

- Work in the existing worktree: <worktree path from planner JSON>
- Read `.pair/CONTEXT.md` FIRST, then `.pair/PLAN.md`. The context brief is a complete handoff from the agent that already explored this repo: the files that matter, real signatures with line numbers, entry points, conventions, exact test/lint commands, and dead ends already ruled out.
- **Do NOT re-explore the codebase.** No broad grep/glob sweeps, no reading files end-to-end to get oriented, no re-deriving what the brief already states — that work is already paid for. Open a file only when (a) you are editing it, (b) the brief lists it under `Not Yet Read` and you need it, or (c) the brief is demonstrably wrong about it — and then read the specific region, not the whole file. If the brief has a gap that blocks you, note it in `brief_gaps` in your return JSON so the planner's brief can be fixed; do not silently fall back to re-exploring.
- `.pair/PLAN.md` has been reviewed against real code by an independent reviewer; treat its Diagnosis, Files & Line Numbers, and Side-Effects Trace as decided.
- Unresolved concerns carried from plan review (must be handled or explicitly noted in the PR body): <unresolved[] from planner JSON, or 'none'>
- Write the failing test named in the Test Plan FIRST, confirm it fails for the stated reason.
- Implement the fix/feature with minimal changes, exactly as the plan specifies.
- Run tests (new test passes, full suite passes) and the linter/type checker.
- Update CHANGELOG.md if one exists (add entry under [Unreleased]).
- Stage specific files (never `git add -A`). Never stage `.pair/`.
- Create an atomic conventional commit: fix|feat(scope): description - Fixes #XX (NO Co-Authored-By, no Claude/Anthropic attribution).
- Push and create a PR linked to the issue (no Claude attribution in the body). If there were unresolved plan-review concerns, list them under `## Unresolved Codex concerns`.

ESCALATION: if implementing reveals the plan is wrong (root cause misidentified, the specified change is impossible or would break a caller the plan didn't consider), STOP. Do not improvise a different fix. Return status `plan_rejected` with the specific reason and the evidence (<file>:<line>). The main loop will send it back to the planner (fable) for revision and then relaunch you.

IMPORTANT - Do NOT remove your worktree when done — Phase 5.5 verification reads `.pair/PLAN.md` from it (gitignored, exists only there). Cleanup happens in the main loop after merge.

IMPORTANT - Return ONLY this minimal JSON (no other text):
{\"issue\": XX, \"pr\": <number|null>, \"worktree\": \"<abs path>\", \"status\": \"success|failed|plan_rejected\", \"brief_gaps\": [\"<what the context brief was missing or wrong about>\"], \"error\": \"<short error / rejection reason if not success>\"}"
```

**On `brief_gaps`:** non-blocking, but log them in the wave summary. A recurring gap means the planner's `.pair/CONTEXT.md` template needs a section — fixing it once removes the duplicated reads permanently.

**On `plan_rejected`:** relaunch the planner agent (`fable`) for that issue with the coder's rejection reason appended to the prompt, have it revise `.pair/PLAN.md` directly (no new Codex review), then relaunch the coder. Max 1 round-trip per issue; after that, mark the issue failed and move on.

### Process in Sub-Batches (Context Safety)

If wave has 4+ issues, split into sub-batches of 2. Each sub-batch runs plan → code sequentially per issue, but the two issues in the sub-batch run in parallel:

```
Wave 1 has 6 issues: [#12, #15, #22, #41, #28, #33]

Sub-batch 1: plan #12, #15 in parallel (fable) → code #12, #15 in parallel (opus)
  → Collect minimal results

Sub-batch 2: plan #22, #41 (fable) → code #22, #41 (opus)
  → Collect minimal results

Sub-batch 3: plan #28, #33 (fable) → code #28, #33 (opus)
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

This phase asks a different question than review: not "is the code good?" but **"is the code what the plan said we'd build?"** It runs once per wave, after PRs are created (Phase 5) and BEFORE review/merge (Phase 6).

**Who runs it:** verification is reviewer work, so it runs in a **reviewer agent (`model: "fable"`, fallback `"opus"`)** — one agent per PR, launched **sequentially, one at a time**. This is not fan-out: exactly one agent per PR, no per-claim agents, no refuter agents, no Workflow. The main loop only collects each agent's JSON verdict.

**Cost rule: budget ONE `gh pr diff` per PR plus targeted file reads.** Do not re-review code quality — that is Phase 6's job.

**CRITICAL: do not remove any worktree before this phase completes** — verifiers read `.pair/PLAN.md` from the worktrees (gitignored, exists only there).

### Step 1 — Build the contract list (inside the reviewer agent)

For each successful PR in the wave (from the coder JSON: issue, pr, worktree):
1. Read `<worktree>/.pair/PLAN.md`.
2. Parse it into discrete claims: Acceptance Criteria checkboxes (type `ac`), Files & Line Numbers entries (`file`), Test Plan items (`test`), Side-Effects Trace invariants (`side-effect`). Assign ids like `ac1`, `f1`, `t1`, `s1`.
3. **Fallback:** if PLAN.md is missing or its ACs are generic boilerplate, use the issue's own acceptance criteria as the contract and note it in that PR's report header: `> Verified against issue acceptance criteria — no implementation plan existed.`

### Step 2 — Verify each PR in a reviewer agent, one at a time

For each PR in the wave, launch ONE reviewer agent (`model: "fable"`, fallback `"opus"`) with the instructions below. Wait for it to return before launching the next PR's reviewer. The agent returns only:

```json
{"pr": 45, "issue": 12, "verdict": "PLAN_MET|PLAN_NOT_MET", "counts": {"matched": 5, "diverged": 1, "missing": 0, "unplanned": 1},
 "failed_claims": [{"id": "t1", "text": "...", "why": "..."}],
 "carry_to_review": ["<diverged/unplanned item worth scrutiny>"]}
```

Reviewer agent instructions:

1. Read the diff once: `gh pr diff <pr>`. That single read is the evidence base for every claim on that PR.
2. Walk the claim list and classify each claim:
   - **MATCHED** — implemented as the plan stated
   - **DIVERGED** — implemented, but differently than planned; note exactly how
   - **MISSING** — not implemented at all
   - **UNVERIFIABLE** — cannot be determined from the code
3. Cite `file:line` evidence. Only open a worktree file when the diff alone can't settle a claim — and check `.pair/CONTEXT.md` first, since the planner's brief already records signatures, call sites, and conventions for the files that matter. When you do open a file, read the specific region, not the whole file.
4. Before marking a claim DIVERGED or MISSING, re-check the diff for the change under a different name or location. A rename or a move is not a miss.
5. Reverse-trace in the same pass: note any hunk that maps to no claim — those are the unplanned changes. Collapse mechanical noise (imports, formatting, lockfiles) into one line.
6. Post the Implementation Report (Step 3) on the PR, then return the JSON verdict. Do NOT fix anything — the reviewer does not write code.

Then move to the next PR. Reviewer agents never run in parallel with each other, and they never spawn sub-agents.

### Step 3 — Per-PR Implementation Report

The reviewer agent posts one report per PR (`gh pr comment <pr> --body "<report>"`):

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
- **PLAN_NOT_MET:** at least one ❌ on an `ac` or `test` claim. Attempt ONE fix cycle, respecting role separation: relaunch a **coder agent (`opus`)** in that worktree with the reviewer's `failed_claims[]` and instructions to apply only the missing pieces, commit and push; then relaunch the **reviewer agent (`fable`)** to re-verify **only the failed claims** (not the whole list). If still failing → the PR is **blocked from auto-merge** in Phase 6; leave it open with the report comment as the record and move on.

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

- **Default (full review):** Uses the Claude↔Codex review loop — Codex reviews the PR, a **reviewer agent (`fable`)** triages the findings, a **coder agent (`opus`)** applies the accepted fixes, repeat until Codex approves (max 15 iterations). Thorough (~5-15 min per PR). Codex receives iteration history so it won't re-raise dismissed issues.
- **`--basic-review` mode:** A **reviewer agent (`fable`)** reviews the diff for breaking changes, regressions, missing changelog, etc. Fast (~1-2 min per PR). Any fixes it asks for are applied by a coder agent (`opus`).

**Verification gate (from Phase 5.5):** a PR marked PLAN_NOT_MET is NEVER auto-merged, regardless of review outcome — review it anyway (the findings are still useful), but leave it open with a comment pointing at the Implementation Report. Include each PR's 🔀 diverged and ➕ unplanned items in the review prompt.

### Review Loop

For each PR created in this wave, do this loop:

```
for each PR in [#45, #46, #47]:

  1. REVIEW the PR:

     If full review (default):
       Run the full Claude↔Codex review loop for this PR:

       a. Get the PR number
       b. Execute the /full-review workflow inline, with roles split across separate agents:
          - Get PR info and checkout the branch
          - Initialize iteration history
          - Run Codex review via `codex exec review - --ephemeral --json` with the prompt piped on stdin (including iteration history context on rounds 2+); do NOT use `--full-auto`
          - Use Codex's default model only; do not pass `--model` or `-c model=...`
          - REVIEWER agent (model: "fable", fallback "opus"): parse the review, triage each [P1]/[P2]
            into ACCEPT (real, must fix) or DISMISS (with reason), and emit a precise fix list
            (<file>:<line> — what to change — why). It does not edit files.
            The fix list must be SELF-CONTAINED: exact file, exact line/region, and the current
            code being changed — so the coder can act without re-reading to locate the site.
          - CODER agent (model: "opus"): apply exactly the ACCEPTed fixes, run tests, commit, push.
            Works from the fix list plus `.pair/CONTEXT.md`; does not re-explore the codebase.
            If a fix cannot be applied as specified, it returns the reason instead of improvising.
          - Update iteration history with outcomes (FIXED/DISMISSED) plus the dismissal reasons
          - Repeat until Codex approves or 15 iterations

       c. Record the final result (approved / max iterations reached)

       NOTE: the loop's fix-commit-push cycle is done by the opus coder; the
       accept/dismiss judgement is always fable's. After the loop completes, the PR is
       either clean (Codex approved) or has been iterated to convergence.

     If --basic-review mode:
       Launch a reviewer agent (model: "fable", fallback "opus") running /review-changes
       This checks: changelog, debug code, secrets, breaking changes, regressions
       Any required fix is applied by a coder agent (model: "opus")

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

# Model routing (defaults): plans + reviews on fable (fallback opus), code on opus
/drain-issues

# Force everything onto opus (e.g. fable is degraded)
/drain-issues --plan-model=opus --review-model=opus

# Cheaper planning, keep review quality
/drain-issues --plan-model=sonnet
```
