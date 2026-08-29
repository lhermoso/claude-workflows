---
allowed-tools: Bash(git:*), Bash(gh:*), Bash(grep:*), Bash(find:*), Bash(cat:*), Bash(npm:*), Bash(cargo:*), Bash(pnpm:*), Task
argument-hint: <issue-description OR issue-number> [--no-plan-review] [--basic-review] [--no-verify] [--plan-model=M] [--code-model=M] [--review-model=M]
description: Full pipeline: create issue (if needed), plan-review it (one Codex pass), fix it, verify implementation against plan (drift report), create PR, full Claude↔Codex review. Plans and reviews run on claude-fable-5 (fallback opus), code is written by claude-opus-5. ALL quality gates ON by default — use --no-plan-review / --basic-review / --no-verify to opt out.
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

**Default quality gates (all ON unless explicitly opted out in `$ARGUMENTS`):**

| Gate | Default | Opt-out flag |
|------|---------|--------------|
| Plan Review (one Codex pass, before coding) | ON | `--no-plan-review` |
| Verification Phase (inline plan-adherence + Implementation Report) | ON | `--no-verify` |
| Full Claude↔Codex review loop on the PR | ON | `--basic-review` (falls back to fast diff review) |

The legacy `--plan-review` / `--full-review` flags are accepted but redundant — they are now the default behavior.

---

## Model Routing (applies to every Claude-side agent in this pipeline)

| Role | Covers | Model | Fallback |
|------|--------|-------|----------|
| **Planner** | root-cause investigation, writing and revising `.pair/PLAN.md`, running the single Codex plan review and absorbing its findings | `fable` (claude-fable-5) | `opus` |
| **Reviewer** | Verification Phase + Implementation Report, basic diff review, triaging Codex `[P1]`/`[P2]` findings, improvement-pass triage, merge decision | `fable` (claude-fable-5) | `opus` |
| **Coder** | failing test, implementation, lint/test runs, CHANGELOG, commits, PR creation, applying accepted review fixes and improvement passes | `opus` (claude-opus-5) | — |

Overrides: `--plan-model=M`, `--code-model=M`, `--review-model=M` (`fable\|opus\|sonnet\|haiku`).

Rules:

1. **Every `Task` launch passes an explicit `model`.** Never inherit the session model.
2. **Fable fallback:** if a launch with `model: fable` fails because the model is unavailable/invalid/over quota, retry the *identical* launch once with `model: opus` and log `⚠️ fable unavailable — <role> downgraded to opus`. Never downgrade a planner or reviewer to a smaller model (`sonnet`/`haiku`).
3. **Coders never plan.** The coder receives a finished `.pair/PLAN.md`. If it concludes the plan is wrong, it stops and returns `plan_rejected` with evidence — the planner (fable) revises, then the coder resumes.
4. **Reviewers never write code.** A reviewer emits verdicts and a precise fix list; a coder applies it.
5. **Codex is unchanged** — external adversarial reviewer on its own default model. Never pass `--model` / `-c model=...` to Codex.
6. **Subagents run Codex in the FOREGROUND.** Any agent launched via `Task` (planner, reviewer, coder) must invoke `codex exec` as a **blocking** call and stay in the same turn until it returns. Never start Codex with `run_in_background` and then end the turn waiting for a task notification — **a subagent is not woken by its own background task**, so the turn simply ends and the agent sits idle until the orchestrator notices (observed: ~20 min lost per planner). If something is backgrounded anyway, poll it with `BashOutput` in a loop **within the same turn** until it exits. Only the main pipeline loop may background work and rely on being re-invoked.

---

## Mode A: Fix Existing Issue

If input is an issue number:

1. **Skip to fixing** - Issue already exists
2. Proceed directly to the Plan Phase below

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

ENHANCED_PROBLEM=$(printf '%s' "$ENHANCE_PROMPT" | codex exec - -s read-only --ephemeral --json 2>/dev/null | python3 -c "
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

## Plan Phase (Planner Subagent — model: `fable`, fallback `opus`)

Launch a dedicated **planner** agent. It investigates and produces a reviewed plan; it writes no production code.

```
Use the Task tool with model: "fable" (on failure retry once with model: "opus"):

"Plan the fix for GitHub issue #<number> in this repository. You are the PLANNER — do not implement, do not commit, do not open a PR. Your deliverable is a reviewed plan.

ENHANCED PROBLEM STATEMENT (pre-analyzed — use this as your primary guide):
<insert $ENHANCED_PROBLEM here if available, otherwise omit this section>

Instructions:
1. Detect default branch: git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo 'main'
2. Create a git worktree from origin/<default-branch>
3. Fetch the full issue including all comments: `gh issue view <number> --json number,title,body,labels,comments`
   Read the issue body AND all comments — comments often contain reproduction steps, clarifications, or constraints that are critical to the correct solution.
4. Investigate the codebase to find ROOT CAUSE — do NOT accept surface-level fixes (z-index hacks, retry loops, overflow-hidden). Ask 'why does this happen?' until you reach the actual cause.
5. Specify the failing test that reproduces the bug (exact path + assertion). The coder writes it; you specify it.
6. Create implementation plan in `.pair/PLAN.md` at worktree root. Use these EXACT sections — Codex review depends on this structure:

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
   - One paragraph naming the weakest assumption in this plan.
     Codex reviews this paragraph FIRST. Be honest — overconfidence here hides bugs.
   ```

   The `Side-Effects Trace` section is non-negotiable. Without it, plan review catches diagnosis errors but misses fix-breaks-something-else errors (the highest-value class of bug Codex catches).

   **Plan review runs by DEFAULT:** after writing `.pair/PLAN.md`, run the single-pass Plan Review below before handing off.
   **Only if `--no-plan-review` was passed:** skip it and hand off the unreviewed plan.
7. Write a CONTEXT BRIEF to `.pair/CONTEXT.md` at worktree root. This is the handoff that stops the coder from re-reading everything you just read — without it, every file you opened gets opened again from a cold context, which is the single largest source of duplicated tokens in this pipeline. Use these sections:

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
8. Do NOT implement, commit, or open a PR — a separate coder agent does that from your plan.
9. Do NOT remove the worktree. The coder and the verifier both need it.
10. Return the absolute worktree path, the plan path, the context-brief path, the plan-review outcome, and any unresolved `[BUG]` items as JSON:
   {"issue": <n>, "worktree": "<abs path>", "plan": "<abs path>", "context_brief": "<abs path>", "plan_review": "reviewed|skipped|unavailable", "unresolved": ["..."], "status": "success|failed", "error": "..."}

PLAN REVIEW (default — skipped only if --no-plan-review was passed):

Goal: ONE adversarial Codex pass over the finished plan, checked against the **actual code in the worktree** — not just plan-shape correctness. Codex reviews once, you absorb the findings into `.pair/PLAN.md`, then the coder starts. There is NO re-review round: the revised plan is final.

**Bookkeeping:**
- Run Codex from the worktree root so it reads real files.
- Write the Codex output to `.pair/REVIEW.md` under a `## Codex Plan Review` header.
- **Run Codex in the FOREGROUND — it is a blocking call.** Do NOT use `run_in_background` and do NOT end your turn waiting for a notification: you are a subagent, nothing will wake you, and the plan review stalls until the pipeline notices. Normal runtime is 3–10 min — wait it out in this turn. If you background it anyway, poll `BashOutput` in a loop in this same turn until the process exits.
- Always pipe the prompt via stdin (per CLAUDE.md — large prompts as positional args silently hang).
- Capture stderr — empty stdout ≠ approval. Retry ONCE on empty output; if the second attempt is also empty, log a warning, set `plan_review: "unavailable"`, and hand the unreviewed plan to the coder.

```bash
CODEX_ERR=$(mktemp -t codex-err-XXXX.log)
PLAN=$(cat .pair/PLAN.md)

REVIEW_PROMPT="You are reviewing an implementation plan against the actual code in this repo. Cover the diagnosis AND the proposed fix in this single pass — there will be no second round.

ISSUE:
$ISSUE_CONTEXT

PLAN:
$PLAN

YOUR TASK:

A. DIAGNOSIS
1. Read the 'What I Am Most Likely Wrong About' paragraph FIRST. Take it seriously.
2. For EVERY <file>:<line> reference in the Diagnosis section, read the file and verify the claim.
3. Identify symptoms misread as root cause.
4. Identify observability traps the plan missed (states that look healthy but aren't).

B. FIX IMPACT
5. For every function the plan modifies, find all call sites. What assumptions break for callers not listed in the plan?
6. For every new code path, identify shared mutable state, dedup keys, cache entries, locks. Find asymmetries (e.g. set-membership check on read but not on write, or vice versa).
7. For every helper the plan reuses, check whether that helper has guards/early-returns/retro-windows that would still block the fix.
8. For every new code path, identify which existing tests cover it and which do not.
9. Find at least one of: incorrect line reference, side-effect not in plan, helper/guard that would still block the fix, dedup/cache asymmetry, missing lock around shared mutable state, lifecycle issue (detached task without supervision, etc.). If after honest search you find none, say so explicitly.

OUTPUT FORMAT (markdown):
## Diagnosis — Confirmed
- <claim> — verified at <file>:<line>
## Diagnosis — Corrections
- [WRONG] <claim> — actual mechanism is <X> at <file>:<line>
## Diagnosis — Missing
- <observability trap or co-existing failure mode> at <file>:<line>
## Fix — Confirmed
- <element of fix> — traced, no side-effects found
## Fix — Bugs Introduced
- [BUG] <description> — at <file>:<line> — why it breaks: <X> — proposed correction: <one line>
## Fix — Missing From Plan
- <missing step / invariant / test> — at <file>:<line>
## Sharper Alternative (optional)
- If a materially simpler or safer approach exists, describe it in 3-5 bullets. Otherwise omit this section.

Be specific. Cite line numbers. No vague 'consider edge cases'."

printf '%s' "$REVIEW_PROMPT" | codex exec - -s read-only --ephemeral --json 2> "$CODEX_ERR"
```

Parse the last `item.completed` / `agent_message` from the JSONL output. Write it to `.pair/REVIEW.md`.

---

**Absorb the review — you do this yourself, with NO second Codex call:**

- Every `[WRONG]` item → rewrite the Diagnosis section of `.pair/PLAN.md`.
- Every `[BUG]` item → rewrite the Side-Effects Trace and Files & Line Numbers sections.
- Every `Fix — Missing From Plan` item → add that step / invariant / test to the plan.
- A `Sharper Alternative` you accept → replace the Proposed Fix, and record why under "Why this and not <alternative>".
- Anything you deliberately reject → append a one-line reason to `.pair/REVIEW.md` under `## Dismissed`, and list it in `unresolved[]` so the coder records it in the PR body under `## Unresolved Codex concerns`.

Do NOT re-run Codex on the revised plan. Set `plan_review: "reviewed"` and hand off to the coder.

---

After the review is absorbed the plan is final. Do NOT commit `.pair/` — it is gitignored working-notes scratch and must stay local to the worktree. Never `git add -f` it. If the pair-session reasoning is worth preserving in the PR record, mirror a concise summary into the PR body instead. Then return the planner JSON from step 10. `.pair/CONTEXT.md` follows the same rule: local to the worktree, never committed, and never deleted before the Verification Phase has run.

Work autonomously. Make reasonable decisions. Only ask if truly blocked."
```

**Wait for the worktree path + plan path from the planner.**

---

## Implementation Phase (Coder Subagent — model: `opus`)

Launch a **coder** agent in the planner's worktree. The plan is the contract.

```
Use the Task tool with model: "opus":

"Implement GitHub issue #<number> from an already-reviewed plan. You are the CODER — do not re-plan, do not redesign.

1. Work in the existing worktree: <worktree path from planner JSON>
2. Read `.pair/CONTEXT.md` FIRST, then `.pair/PLAN.md`. The context brief is a complete handoff from the agent that already explored this repo: the files that matter, real signatures with line numbers, entry points, conventions, exact test/lint commands, and dead ends already ruled out.
3. **Do NOT re-explore the codebase.** No broad grep/glob sweeps, no reading files end-to-end to get oriented, no re-deriving what the brief already states — that work is already paid for. Open a file only when (a) you are editing it, (b) the brief lists it under `Not Yet Read` and you need it, or (c) the brief is demonstrably wrong about it — and then read the specific region, not the whole file. If the brief has a gap that blocks you, report it in `brief_gaps` on return; do not silently fall back to re-exploring.
4. `.pair/PLAN.md`'s Diagnosis, Files & Line Numbers, and Side-Effects Trace were reviewed against real code by an independent reviewer — treat them as decided.
5. Unresolved concerns carried from plan review (handle or note explicitly in the PR body): <unresolved[] from planner JSON, or 'none'>
6. Write the failing test named in the Test Plan FIRST and confirm it fails for the stated reason
7. Implement the fix with minimal changes, exactly as the plan specifies — use the exact commands from the brief's `Commands` section rather than discovering them
8. Run tests — the new test should pass, and the full test suite should pass
9. Run the linter/type checker
10. Update CHANGELOG.md at the REPO ROOT if one exists (add entry under [Unreleased]). Check for correct file path first.
11. Stage specific files (never use git add -A). Never stage `.pair/`.
12. Create a commit with conventional format: fix|feat(scope): description - Fixes #<number>
   DO NOT include any Co-Authored-By lines or Claude/Anthropic attribution
13. Push the branch and create a PR using gh pr create
   DO NOT include any 'Generated by Claude' or similar attribution in PR body
   Verify any labels exist before using them: gh label list --json name --jq '.[].name'
   If there were unresolved plan-review concerns, list them under `## Unresolved Codex concerns`
14. Return the PR number, the absolute worktree path, and `brief_gaps` (anything the context brief was missing or wrong about — non-blocking, but it tells the planner what to add next time). Do NOT remove the worktree — the Verification Phase reads `.pair/PLAN.md` from it (gitignored, exists only there).

ESCALATION: if implementing reveals the plan is wrong (root cause misidentified, the specified change is impossible, or it would break a caller the plan didn't consider), STOP. Do not improvise a different fix. Return {"status": "plan_rejected", "reason": "...", "evidence": "<file>:<line>"}. The pipeline will send it back to the planner (fable) and relaunch you.

GUARDRAILS:
- NEVER push directly to master/main
- NEVER add redirects or middleware unless explicitly requested
- Implement the root-cause fix from the plan (no surface-level patches)
- Run full test suite AND linter before committing

Work autonomously within the plan. Only ask if truly blocked."
```

**On `plan_rejected`:** relaunch the planner (`fable`) with the coder's reason appended; it revises `.pair/PLAN.md` directly (no new Codex review), then relaunch the coder. Max 1 round-trip; after that stop and report **NEEDS ATTENTION**.

**Wait for PR number + worktree path from the coder.**

---

## Verification Phase (Plan-Adherence Report) — DEFAULT

Runs after the Implementation Phase, before the Review Phase. Skip only if `--no-verify` was passed.

The review loop asks "is the code good?". This phase asks a different question: **"is the code what we said we'd build?"** It verifies the implementation claim-by-claim against `.pair/PLAN.md`, then produces an **Implementation Report**: what was implemented as planned, what diverged, what's missing, and what changed without being in the plan.

**Who runs it:** verification is reviewer work, so it runs in **exactly one reviewer agent** (`model: "fable"`, fallback `"opus"`). One agent, no fan-out, no per-claim agents, no Workflow. The reviewer posts the report and returns a JSON verdict; it does not edit code.

**Cost rule: this phase is ONE diff read plus targeted file reads.** Do not re-review the code for quality — that is the Review Phase's job.

### Step 1 — Gather the contract

- Use the worktree path + PR number returned by the coder subagent.
- Read `.pair/PLAN.md` from the worktree root.
- **Fallback:** if `PLAN.md` is missing, or its Acceptance Criteria are generic boilerplate ("Implementation complete", "Tests pass"), use the enhanced problem statement's success criteria (or the issue's own ACs) as the contract instead — and say so in the report header: `> Verified against issue acceptance criteria — no implementation plan existed.`

### Step 2 — Parse the plan into discrete claims (inside the reviewer agent)

Extract one claim per:
- Acceptance Criteria checkbox → type `ac` (ids `ac1`, `ac2`, ...)
- Files & Line Numbers entry → type `file` (ids `f1`, ...)
- Test Plan item → type `test` (ids `t1`, ...)
- Side-Effects Trace invariant → type `side-effect` (ids `s1`, ...)

### Step 3 — Verify against the diff (reviewer agent)

1. Read the diff once: `gh pr diff <pr>`. That single read is the evidence base for every claim.
2. Walk the claim list top to bottom and classify each one:
   - **MATCHED** — implemented as the plan stated
   - **DIVERGED** — implemented, but differently than planned; note exactly how
   - **MISSING** — not implemented at all
   - **UNVERIFIABLE** — cannot be determined from the code
3. Cite `file:line` evidence for each verdict. Only open a file from the worktree when the diff alone can't settle a claim (e.g. the claim is about behavior in unchanged surrounding code) — and check `.pair/CONTEXT.md` first, since the planner's brief already records signatures, call sites, and conventions for the files that matter. When you do open a file, read the specific region, not the whole file.
4. Before marking a claim DIVERGED or MISSING, re-check the diff for the change under a different name or location — a rename or a move is not a miss. That single re-check replaces the old adversarial confirm step.
5. Reverse-trace in the same pass: as you read hunks, note any hunk that maps to no claim. Those are the unplanned changes. Collapse mechanical noise (imports, formatting, lockfiles) into one line.

### Step 4 — Render and post the Implementation Report

From the reviewer's verdicts, build:

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

Post it on the PR: `gh pr comment <pr> --body "<report>"` — this is the durable record of plan-vs-reality.

### Step 5 — Decision

- **Any ❌ on an `ac` or `test` claim** → the implementation does not meet its own contract. Relaunch a **coder agent (`opus`)** in the worktree with the failed claims only — apply the missing piece, commit, push — then relaunch the **reviewer agent (`fable`)** to re-verify **only the failed claims** (not the whole list). Max 2 verify-fix cycles; after that, proceed but mark the final pipeline status **NEEDS ATTENTION** and leave the report as the record.
- **🔀** → non-blocking. The divergence is documented; if the implementation took a *better* path than the plan, fine — the point is it's no longer silent.
- **➕ unplanned changes** → non-blocking, but they are exactly what the Review Phase should scrutinize first — carry them into the review prompt.

---

## Review Phase

### Review Mode Selection

There are two review modes. **Full review is the DEFAULT.** Use basic only if `--basic-review` is in `$ARGUMENTS`:

- **Default (full review):** Uses the Claude↔Codex review loop — Codex reviews the PR, a **reviewer agent (`fable`)** triages the [P1]/[P2] findings, a **coder agent (`opus`)** applies the accepted fixes and pushes, repeat until Codex approves (max 15 iterations). Codex receives iteration history so it won't re-raise dismissed issues. Thorough (~5-15 min).
- **`--basic-review` mode:** A **reviewer agent (`fable`)** reviews the PR diff for breaking changes, regressions, debug code, secrets, etc. Fast (~1-2 min). Fixes it requires are applied by a coder agent (`opus`).

If the Verification Phase produced an Implementation Report, include its 🔀 diverged and ➕ unplanned items in the review prompt — they are the highest-priority things for the reviewer to scrutinize.

### If `--basic-review` mode:

Run in a reviewer agent (`model: "fable"`, fallback `"opus"`):

1. Get the full PR diff: `gh pr diff <pr-number>`
2. Check for:
   - Code changes match the issue requirements
   - No breaking changes (function signatures, API contracts, exports)
   - No debug code (console.log, print statements)
   - No hardcoded secrets or credentials
   - Tests are included/passing
   - Changelog updated (if project has CHANGELOG.md)
   - No regressions in related functionality

Based on the reviewer's verdict:
- If **SAFE TO MERGE** → Proceed to merge
- If **NEEDS CHANGES** → hand the fix list to a coder agent (`opus`) to apply, or stop and report if the changes are out of scope

### If full review (default):

Run the full Claude↔Codex review loop for this PR, with roles split across separate agents:

1. Get PR info: `gh pr view <pr-number> --json title,body,headRefName,baseRefName,files`
2. Checkout the PR branch: `gh pr checkout <pr-number>`
3. Determine base branch: `gh pr view <pr-number> --json baseRefName -q '.baseRefName'`
4. Initialize `ITERATION_HISTORY = ""`
5. **Loop (max 15 iterations):**
   a. Build Codex prompt:
      - Iteration 1: `review`
      - Iteration N>1: Include `ITERATION_HISTORY` with instructions to skip dismissed/fixed issues
   b. Run Codex review with the default model only — pipe the prompt via stdin: `printf '%s' "$REVIEW_PROMPT" | codex exec review - --ephemeral --json --title "..." 2> "$CODEX_ERR"`
      Omit `--base` (it is mutually exclusive with a custom prompt) — instruct Codex in-prompt to diff `HEAD` vs `origin/$BASE_BRANCH`. Do NOT use `--full-auto` (errors on the `review` subcommand). Do not pass `--model` or `-c model=...`. Capture stderr; empty stdout ≠ approval.
   c. Parse JSONL output for the last `agent_message` text
   d. If no [P1]/[P2] issues → **APPROVED**, break
   e. **Reviewer agent (`model: "fable"`, fallback `"opus"`):** triage each [P1]/[P2] into ACCEPT (real, must fix) or DISMISS (with reason) and emit a precise fix list (`<file>:<line>` — what to change — why). It does not edit files. The fix list must be **self-contained**: exact file, exact line/region, and the current code being changed, so the coder can act without re-reading to locate the site.
   f. **Coder agent (`model: "opus"`):** apply exactly the ACCEPTed fixes, run tests, commit, push. Works from the fix list plus `.pair/CONTEXT.md`; does not re-explore the codebase. If a fix can't be applied as specified, return the reason instead of improvising.
   g. Update `ITERATION_HISTORY` with outcomes (FIXED/DISMISSED/NOTED) plus dismissal reasons
   h. Repeat

Based on the result:
- If **Codex approved** → Proceed to improvement passes
- If **Max iterations with unresolved [P1]s** → List remaining issues and stop

---

## Improvement Passes (Post-Implementation)

After the review passes (full or basic), run up to **2 improvement passes** on the finished implementation. This is different from the review loop — instead of finding bugs, Codex generates a fresh prompt focused on improving the *quality* of the existing output.

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

IMPROVEMENT_PROMPT=$(printf '%s' "$IMPROVE_PROMPT" | codex exec - -s read-only --ephemeral --json 2>/dev/null | python3 -c "
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

Otherwise: a **reviewer agent (`fable`)** decides which of Codex's suggestions to accept (reject anything that is a rewrite, scope creep, or contradicts the plan), then a **coder agent (`opus`)** applies the accepted ones — checkout the PR branch, edit the named files, run tests, commit (`improve: quality pass N`), push.

---

## Final Output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Pipeline Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Issue:  #<number> - <title>
PR:     #<pr-number>
Plan:   <plan-review result: reviewed | skipped | unavailable>
Report: <N as planned · N diverged · N missing · N unplanned | skipped>
Status: <review result>
Models: plan=<fable|opus|…> · code=<opus|…> · review=<fable|opus|…>
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
- **`fable` unavailable / invalid model:** retry the same launch once with `model: "opus"`, log the downgrade, continue. Never downgrade a planner or reviewer to a smaller model (`sonnet`/`haiku`).
- **Coder returns `plan_rejected`:** send the reason back to the planner (`fable`), which revises the plan directly, then relaunch the coder. Max 1 round-trip, then report NEEDS ATTENTION.
- **Subagent fails to create worktree:** Check if directory already exists, or if branch name conflicts
- **Tests fail:** Report which tests failed and the error output, suggest manual investigation
- **PR creation fails:** Check if branch was pushed, if remote is accessible

---

## Notes

- This command uses subagents to maintain context isolation
- Roles are split across separate agents: planning and reviewing on `fable` (fallback `opus`), code on `opus`. A single agent never both plans and implements — that separation is what makes the plan-adherence report meaningful
- Each phase runs with fresh context (no /clear needed)
- Worktrees ensure parallel work doesn't conflict — and must NOT be removed until the Verification Phase has read `.pair/PLAN.md` from them
- Review happens automatically but human can override
- All quality gates are ON by default. Fastest escape hatch: `--no-plan-review --no-verify --basic-review` (roughly the old default behavior)
