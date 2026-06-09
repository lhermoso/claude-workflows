---
allowed-tools: Bash(git:*), Bash(gh:*), Bash(grep:*), Bash(find:*), Bash(cat:*), Bash(npm:*), Bash(cargo:*), Bash(pnpm:*), Task, Workflow
argument-hint: <issue-description OR issue-number> [--no-plan-review] [--basic-review] [--no-verify]
description: Full pipeline: create issue (if needed), plan-review it (Claude↔Codex two-pass), fix it, verify implementation against plan (Workflow drift report), create PR, full Claude↔Codex review. ALL quality gates ON by default — use --no-plan-review / --basic-review / --no-verify to opt out.
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
| Plan Review Loop (Codex two-pass, before coding) | ON | `--no-plan-review` |
| Verification Phase (plan-adherence Workflow + Implementation Report) | ON | `--no-verify` |
| Full Claude↔Codex review loop on the PR | ON | `--basic-review` (falls back to fast diff review) |

The legacy `--plan-review` / `--full-review` flags are accepted but redundant — they are now the default behavior.

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

   **Plan review runs by DEFAULT:** after writing `.pair/PLAN.md`, run the Plan Review Loop before implementing anything (see Plan Review Loop section below).
   **Only if `--no-plan-review` was passed:** skip the loop and proceed directly to step 7.
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
14. Return the PR number AND the absolute worktree path when done. Do NOT remove the worktree — the Verification Phase reads `.pair/PLAN.md` from it (gitignored, exists only there).

PLAN REVIEW LOOP (default — skipped only if --no-plan-review was passed):

Goal: replicate the Claude↔Codex pair-review pattern that produces sharpened plans (see polymarkets-weather#231 for the reference shape). The current plan is reviewed against the **actual code in the worktree**, not just plan-shape correctness.

Two distinct passes are run in sequence. Merging them dilutes both.

**Bookkeeping:**
- All Codex invocations run from worktree root so codex reads real files.
- Append every Codex output to `.pair/REVIEW.md` with a `## Round N — <pass-name>` header. Pass the file in subsequent prompts so Codex doesn't re-raise dismissed issues.
- Always pipe the prompt via stdin (per CLAUDE.md — large prompts as positional args silently hang).
- Capture stderr — empty stdout ≠ approval.

---

**Pass A — Diagnosis verification (1 round)**

Verify the root cause is real before discussing any fix. If diagnosis is wrong, the fix is irrelevant.

```bash
CODEX_ERR=$(mktemp -t codex-err-XXXX.log)
PLAN=$(cat .pair/PLAN.md)

DIAG_PROMPT="You are reviewing the DIAGNOSIS section of an implementation plan against the actual code in this repo.

ISSUE:
$ISSUE_CONTEXT

PLAN:
$PLAN

YOUR TASK — diagnosis only, ignore the proposed fix for now:
1. Read the 'What I Am Most Likely Wrong About' paragraph first. Take it seriously.
2. For EVERY <file>:<line> reference in the Diagnosis section, read the file and verify the claim.
3. Identify symptoms misread as root cause.
4. Identify observability traps the plan missed (states that look healthy but aren't).

OUTPUT FORMAT (markdown):
## Diagnosis — Confirmed
- <claim> — verified at <file>:<line>
## Diagnosis — Corrections
- [WRONG] <claim> — actual mechanism is <X> at <file>:<line>
## Diagnosis — Missing
- <observability trap or co-existing failure mode> at <file>:<line>
## Verdict
DIAGNOSIS_CONFIRMED | DIAGNOSIS_NEEDS_REVISION

Be specific. Cite line numbers. If you find no issues after honest search, say so explicitly."

printf '%s' "$DIAG_PROMPT" | codex exec - -s read-only --ephemeral --json 2> "$CODEX_ERR"
```

Parse last `item.completed` / `agent_message` from JSONL. Append to `.pair/REVIEW.md`.

If `DIAGNOSIS_NEEDS_REVISION` → revise `.pair/PLAN.md` Diagnosis section, re-run Pass A (max 2 attempts).
If `DIAGNOSIS_CONFIRMED` → proceed to Pass B.

---

**Pass B — Fix-impact trace (max 3 rounds)**

Diagnosis is real. Now check that the proposed fix doesn't break something else. This is where Codex catches the highest-value bugs (e.g. polymarkets-weather#231 Bug 2: dedup asymmetry → duplicate emissions).

```bash
PLAN=$(cat .pair/PLAN.md)
REVIEW_HISTORY=$(cat .pair/REVIEW.md)

FIX_PROMPT="Diagnosis was confirmed. Now review the PROPOSED FIX against the actual code.

ISSUE:
$ISSUE_CONTEXT

PLAN:
$PLAN

PRIOR REVIEW ROUNDS (do not re-raise issues already addressed or dismissed):
$REVIEW_HISTORY

YOUR TASK:
1. For every function the plan modifies, find all call sites. What assumptions break for callers not listed in the plan?
2. For every new code path, identify shared mutable state, dedup keys, cache entries, locks. Find asymmetries (e.g. set-membership check on read but not on write, or vice versa).
3. For every helper the plan reuses, check whether that helper has guards/early-returns/retro-windows that would still block the fix.
4. For every new code path, identify which existing tests cover it and which do not.
5. Find at least one of: incorrect line reference, side-effect not in plan, helper/guard that would still block the fix, dedup/cache asymmetry, missing lock around shared mutable state, lifecycle issue (detached task without supervision, etc.). If after honest search you find none, say so explicitly.

OUTPUT FORMAT (markdown):
## Fix — Confirmed
- <element of fix> — traced, no side-effects found
## Fix — Bugs Introduced
- [BUG] <description> — at <file>:<line> — why it breaks: <X> — proposed correction: <one line>
## Fix — Missing From Plan
- <missing step / invariant / test> — at <file>:<line>
## Sharper Alternative (optional)
- If a materially simpler or safer approach exists, describe it in 3-5 bullets. Otherwise omit this section.
## Verdict
FIX_APPROVED | FIX_NEEDS_REVISION

Be specific. Cite line numbers. No vague 'consider edge cases'."

printf '%s' "$FIX_PROMPT" | codex exec - -s read-only --ephemeral --json 2> "$CODEX_ERR"
```

Parse last `agent_message`. Append to `.pair/REVIEW.md`.

If `FIX_APPROVED` and no `[BUG]` items → exit loop, proceed to implement.
If `[BUG]` items → revise `.pair/PLAN.md` (Side-Effects Trace + Files sections especially), increment round, repeat.
If round reaches 3 → proceed to implement with the current (best) plan, but log unresolved `[BUG]` items in the PR body under `## Unresolved Codex concerns`.

---

After both passes, implement using the final `.pair/PLAN.md`. Do NOT commit `.pair/` — it is gitignored working-notes scratch and must stay local to the worktree. Never `git add -f` it. If the pair-session reasoning is worth preserving in the PR record, mirror a concise summary into the PR body instead.

GUARDRAILS:
- NEVER push directly to master/main
- NEVER add redirects or middleware unless explicitly requested
- Identify ROOT CAUSE before implementing (no surface-level fixes)
- Run full test suite AND linter before committing

Work autonomously. Make reasonable decisions. Only ask if truly blocked."
```

**Wait for PR number + worktree path from subagent.**

---

## Verification Phase (Plan-Adherence Report) — DEFAULT

Runs after the Fix Phase, before the Review Phase. Skip only if `--no-verify` was passed.

The review loop asks "is the code good?". This phase asks a different question: **"is the code what we said we'd build?"** It verifies the implementation claim-by-claim against `.pair/PLAN.md` using a fan-out Workflow, then produces an **Implementation Report**: what was implemented as planned, what diverged, what's missing, and what changed without being in the plan.

### Step 1 — Gather the contract

- Use the worktree path + PR number returned by the fix subagent.
- Read `.pair/PLAN.md` from the worktree root.
- **Fallback:** if `PLAN.md` is missing, or its Acceptance Criteria are generic boilerplate ("Implementation complete", "Tests pass"), use the enhanced problem statement's success criteria (or the issue's own ACs) as the contract instead — and say so in the report header: `> Verified against issue acceptance criteria — no implementation plan existed.`

### Step 2 — Parse the plan into discrete claims (inline, no agents)

Extract one claim per:
- Acceptance Criteria checkbox → type `ac` (ids `ac1`, `ac2`, ...)
- Files & Line Numbers entry → type `file` (ids `f1`, ...)
- Test Plan item → type `test` (ids `t1`, ...)
- Side-Effects Trace invariant → type `side-effect` (ids `s1`, ...)

### Step 3 — Run the verification workflow

Call the **Workflow tool** with the script below, passing `args: { worktree: "<abs path>", pr: <number>, claims: [{id, type, text}, ...] }`. Design notes: one verifier per claim; adversarial confirm runs ONLY on bad news (DIVERGED/MISSING) so the report doesn't cry wolf; one reverse-trace agent maps diff hunks back to claims to surface unplanned changes.

```js
export const meta = {
  name: 'plan-adherence',
  description: 'Verify implementation against PLAN.md claims and produce drift report',
  phases: [
    { title: 'Verify', detail: 'one agent per plan claim' },
    { title: 'Confirm', detail: 'adversarial check on divergences only' },
    { title: 'Trace', detail: 'map diff hunks back to claims' },
  ],
}
const VERDICT = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['MATCHED', 'DIVERGED', 'MISSING', 'UNVERIFIABLE'] },
    evidence: { type: 'string', description: 'file:line citations' },
    divergence: { type: 'string', description: 'how the implementation differs from the claim (empty if MATCHED)' },
  },
  required: ['status', 'evidence'],
}
const REFUTE = {
  type: 'object',
  properties: { refuted: { type: 'boolean' }, reason: { type: 'string' } },
  required: ['refuted', 'reason'],
}
const UNPLANNED = {
  type: 'object',
  properties: {
    changes: {
      type: 'array',
      items: {
        type: 'object',
        properties: { location: { type: 'string' }, description: { type: 'string' } },
        required: ['location', 'description'],
      },
    },
  },
  required: ['changes'],
}
const { worktree, pr, claims } = args
const claimList = claims.map(c => `- [${c.id}] (${c.type}) ${c.text}`).join('\n')

const verified = await pipeline(claims,
  c => agent(`Verify this implementation-plan claim against the ACTUAL code.

CLAIM (${c.type}): ${c.text}

Worktree with the implementation: ${worktree} — read the real files there.
PR diff: run \`gh pr diff ${pr}\`.

Classify as exactly one of:
- MATCHED: implemented as the plan stated
- DIVERGED: implemented, but differently than planned — describe exactly how in 'divergence'
- MISSING: not implemented at all
- UNVERIFIABLE: cannot be determined from the code

Cite file:line evidence for whatever you conclude.`,
    { label: `verify:${c.id}`, phase: 'Verify', schema: VERDICT }),
  (v, c) => (!v || v.status === 'MATCHED' || v.status === 'UNVERIFIABLE')
    ? ({ claim: c, verdict: v, confirmed: true })
    : parallel([1, 2].map(i => () =>
        agent(`A verifier reviewed PR #${pr} (worktree: ${worktree}) and reported:

CLAIM: ${c.text}
FINDING: ${v.status} — ${v.divergence || v.evidence}

Your job (independent perspective ${i}): try to REFUTE this finding. Read the actual code and prove the claim WAS implemented as planned. Set refuted=true only with file:line proof; if you cannot refute after honest search, set refuted=false.`,
          { label: `confirm:${c.id}`, phase: 'Confirm', schema: REFUTE })))
        .then(votes => ({
          claim: c, verdict: v,
          confirmed: votes.filter(Boolean).filter(x => !x.refuted).length >= 1,
        }))
)

const unplanned = await agent(`Read the full diff of PR #${pr} (run \`gh pr diff ${pr}\`).

PLAN CLAIMS:
${claimList}

For each diff hunk, map it to a claim id. Return ONLY the changes that map to NO claim — these are unplanned changes. Group mechanical noise (imports, formatting, lockfiles) into a single entry.`,
  { label: 'reverse-trace', phase: 'Trace', schema: UNPLANNED })

return {
  verified: verified.filter(Boolean),
  unplanned: unplanned ? unplanned.changes : [],
}
```

### Step 4 — Render and post the Implementation Report

From the workflow's return value, build:

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

Status mapping: `MATCHED` → ✅ · confirmed `DIVERGED` → 🔀 · confirmed `MISSING` → ❌ · `UNVERIFIABLE` → ⚠️. A DIVERGED/MISSING finding that the Confirm step refuted (`confirmed: false`) is reported as ✅ — the verifier was wrong, note it briefly.

Post it on the PR: `gh pr comment <pr> --body "<report>"` — this is the durable record of plan-vs-reality.

### Step 5 — Decision

- **Any confirmed ❌ on an `ac` or `test` claim** → the implementation does not meet its own contract. Fix it in the worktree (apply the missing piece, commit, push), then re-run Steps 3–4. Max 2 verify-fix cycles; after that, proceed but mark the final pipeline status **NEEDS ATTENTION** and leave the report as the record.
- **Confirmed 🔀** → non-blocking. The divergence is documented; if the implementation took a *better* path than the plan, fine — the point is it's no longer silent.
- **➕ unplanned changes** → non-blocking, but they are exactly what the Review Phase should scrutinize first — carry them into the review prompt.

---

## Review Phase

### Review Mode Selection

There are two review modes. **Full review is the DEFAULT.** Use basic only if `--basic-review` is in `$ARGUMENTS`:

- **Default (full review):** Uses the Claude↔Codex review loop — Codex reviews the PR, Claude fixes any [P1]/[P2] issues, commits and pushes, repeat until Codex approves (max 15 iterations). Codex receives iteration history so it won't re-raise dismissed issues. Thorough (~5-15 min).
- **`--basic-review` mode:** Claude reviews the PR diff for breaking changes, regressions, debug code, secrets, etc. Fast (~1-2 min).

If the Verification Phase produced an Implementation Report, include its 🔀 diverged and ➕ unplanned items in the review prompt — they are the highest-priority things for the reviewer to scrutinize.

### If `--basic-review` mode:

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

### If full review (default):

Run the full Claude↔Codex review loop for this PR inline:

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
   e. Fix [P1]/[P2] issues, commit, push
   f. Update `ITERATION_HISTORY` with outcomes (FIXED/DISMISSED/NOTED)
   g. Repeat

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

Otherwise, apply the improvements: checkout the PR branch, read the relevant files, apply the suggested improvements, run tests, commit (`improve: quality pass N`), push.

---

## Final Output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Pipeline Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Issue:  #<number> - <title>
PR:     #<pr-number>
Plan:   <plan-review result: confirmed in N rounds | skipped>
Report: <N as planned · N diverged · N missing · N unplanned | skipped>
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
- Worktrees ensure parallel work doesn't conflict — and must NOT be removed until the Verification Phase has read `.pair/PLAN.md` from them
- Review happens automatically but human can override
- All quality gates are ON by default. Fastest escape hatch: `--no-plan-review --no-verify --basic-review` (roughly the old default behavior)
