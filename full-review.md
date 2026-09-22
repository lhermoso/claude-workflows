---
allowed-tools: Bash, Read, Edit, Write, Grep, Glob, Agent
argument-hint: <pr-number> [--plan=<path/to/PLAN.md>] [--report=<path/to/REPORT.md>] [--no-ci-gate]
description: Nuclear Claude ↔ Codex review loop - Codex reviews the diff against the ORIGINAL REQUIREMENTS and the IMPLEMENTATION PLAN across four gates (correctness, requirements, architecture, security) plus an explicit defect checklist; Claude fixes, repeat until approved; then CI must be green before the PR counts as approved
---

# Full Review: Nuclear Claude ↔ Codex Review Loop

You are entering an automated **nuclear review-fix loop** for **PR #$ARGUMENTS**.

## The Cycle

```
REQUIREMENTS ──► PLAN (fable) ──► DIFF (opus) ──► REVIEW (Codex) ──► FIXES (opus) ──► TESTS / CI
```

This command owns the last three boxes. The reviewer receives **three inputs** every iteration:

1. **Original requirements** — linked issue(s) + PR description.
2. **Implementation plan** — `.pair/PLAN.md` written by the planner, plus the Implementation Report produced by the Verification Phase (plan-vs-diff adherence). Optional: when absent, the architecture gate is N/A and the review runs against requirements only.
3. **The diff** — `HEAD` vs `origin/$BASE_BRANCH`, read by Codex in-sandbox.

"Nuclear" means the review is not code-diff-only. It evaluates the PR across **four gates**, and approval requires **all four** to pass, then **CI must be green**:

1. **Correctness** — does the changed code work, on realistic paths?
2. **Requirements** — does the PR deliver the acceptance criteria of its linked issue(s) and its own stated description?
3. **Architecture** — does the implementation follow the plan's Proposed Fix / Files / Side-Effects Trace, or did it deviate silently? (N/A without a plan)
4. **Security** — does the diff introduce or leave open a realistic vulnerability?

Every gate is also run through the **defect checklist**: race conditions, state inconsistencies, database issues, performance regressions, missing edge cases, missing tests.

## Severity Scale

| Level | Meaning | Blocks approval |
|---|---|---|
| **BLOCKER** | Will break production or lose data on a realistic path; realistic exploit with major blast radius; an explicit requirement is MISSING so the PR does not deliver the issue | yes |
| **HIGH** | Concrete bug on a documented/common path; exploit with limited blast radius or missing authz on a common path; explicit requirement PARTIAL; architecture deviation that changes a contract or invariant; the plan's primary failing test is absent | yes |
| **MEDIUM** | Bug needing uncommon conditions; performance regression without user-visible impact yet; missing edge-case test; hardening / defense-in-depth | no |
| **LOW** | Naming, style, minor cleanup, suggestions | no |

Findings carry an axis tag: `[CORRECTNESS]`, `[AC]`, `[ARCH]`, `[SECURITY]`, `[CI]`. Only BLOCKER and HIGH block; MEDIUM and LOW are recorded and, at most, opened as follow-ups.

## Rules

- **MAX_ITERATIONS = 6** — stop after 6 rounds regardless. If unresolved after 6, remaining items go to a follow-up PR; do not keep looping.
- Each iteration: Codex reviews → if changes requested → you fix & push → Codex reviews again. **Codex never edits code** — it reads, reproduces, and reports. Corrections are proposed in the finding and applied by the coder.
- **Default scope:** prefer fixes limited to files already changed by the PR. Do NOT request or perform unrelated refactors or broad architectural cleanup.
- After fixing, commit and push to the PR branch (do NOT create a new PR).
- **Approval = Codex LGTM AND CI green.** A Codex LGTM with failing, pending, or missing CI is `CI_FAILED` / `CI_MISSING`, never `APPROVED` (Phase 3.5). Only `--no-ci-gate` skips the CI wait, and then the result is reported as `APPROVED_NO_CI`, never `APPROVED`.
- When all gates pass and CI is green, print a summary and stop.

## Completeness / Security Carve-Out (overrides default scope)

The default "stay inside the diff" rule is for *code smells*. It does **not** apply to requirement gaps, plan deviations, or security holes:

- A finding is **NOT** dismissible as "scope expansion" if it is BLOCKER/HIGH and tagged `[AC]`, `[ARCH]`, or `[SECURITY]`.
- You MAY touch non-diff files **only** when the change is the *minimal direct fix* required to (a) satisfy an explicit acceptance criterion, (b) restore an invariant the plan's Side-Effects Trace named, or (c) close a realistic security hole on the PR's affected path. Codex must name the non-diff file and explain why the changed files alone cannot fix it.
- If the required fix is broad, architectural, or risky → do **not** approve and do **not** hack it in. Mark the PR **BLOCKED**, open a follow-up issue with a concrete remediation plan, and report the PR as **incomplete** — never silently approve a half-delivered feature.

## Anti-Escalation Rule

Codex reviews tend to drift: if it raised "X is too weak" in iteration N and you fixed with a stricter X', it will often come back in iteration N+1 with "X' is still too weak, need X''". This is **same-axis escalation** and you should resist it.

When Codex raises a finding on an axis it already raised in a previous iteration:
- If the prior fix was implemented as agreed: **dismiss** the new finding as same-axis escalation. Record in history.
- Only re-engage if Codex points to a concrete, *different* failure mode (not a stricter hypothetical variant of the same concern).

**Exemption:** anti-escalation and the "needs 3+ stacking conditions → MEDIUM" downgrade do **NOT** apply to `[AC]`, `[ARCH]`, or `[SECURITY]` findings. A confirmed unmet criterion, a confirmed plan deviation that breaks an invariant, or a confirmed vulnerability stays blocking regardless of likelihood — never downgrade it to make a round end.

## Phase 0: Setup & Context Gathering

1. Get PR info and checkout the branch:

```bash
PR_NUMBER=$(printf '%s' "$ARGUMENTS" | awk '{print $1}')
gh pr view "$PR_NUMBER" --json title,body,headRefName,baseRefName,files
gh pr checkout "$PR_NUMBER"
```

2. Determine and fetch the base branch:

```bash
BASE_BRANCH=$(gh pr view "$PR_NUMBER" --json baseRefName -q '.baseRefName')
git fetch origin "$BASE_BRANCH"
echo "Base branch: origin/$BASE_BRANCH"
```

3. Capture the PR's title and stated intent — these get baked into the review prompt so Codex can't drift into out-of-scope refactor requests:

```bash
PR_TITLE=$(gh pr view "$PR_NUMBER" --json title -q '.title')
PR_INTENT_ONE_LINE=$(gh pr view "$PR_NUMBER" --json body -q '.body' | awk '/^## Summary/{flag=1;next} /^## /{flag=0} flag && NF' | head -1)
PR_INTENT_ONE_LINE="${PR_INTENT_ONE_LINE:-$PR_TITLE}"
```

4. **INPUT 1a — PR context block** (full description + changed files) into a file:

```bash
PR_CONTEXT_FILE=$(mktemp -t nuclear-pr-context-XXXX.md)
gh pr view "$PR_NUMBER" \
  --json number,title,url,body,baseRefName,headRefName,files \
  --jq '"# PR #\(.number): \(.title)\nURL: \(.url)\nBase: \(.baseRefName)\nHead: \(.headRefName)\n\n## PR Description\n\(.body // "<empty>")\n\n## Changed Files\n\([.files[].path] | join("\n"))"' \
  > "$PR_CONTEXT_FILE"
```

5. **INPUT 1b — linked issue(s) with their acceptance criteria.** `closingIssuesReferences` is the primary source; the body regex is a fallback that also catches `Fixes #N` / `Resolves #N`:

```bash
ISSUE_CONTEXT_FILE=$(mktemp -t nuclear-issue-context-XXXX.md)
ISSUE_NUMBERS=$(
  {
    gh pr view "$PR_NUMBER" --json closingIssuesReferences \
      --jq '.closingIssuesReferences[]?.number'
    gh pr view "$PR_NUMBER" --json body --jq '.body // ""' |
      perl -0777 -ne 'while (/(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#([0-9]+)/ig) { print "$1\n" }'
  } | awk 'NF && !seen[$0]++'
)

: > "$ISSUE_CONTEXT_FILE"
if [ -n "$ISSUE_NUMBERS" ]; then
  while IFS= read -r ISSUE_NUMBER; do
    gh issue view "$ISSUE_NUMBER" \
      --json number,title,url,state,labels,body,comments \
      --jq '"## Issue #\(.number): \(.title)\nURL: \(.url)\nState: \(.state)\nLabels: \([.labels[].name] | join(", "))\n\n\(.body // "<empty>")\n" + (if (.comments | length) > 0 then "\n### Comments\n" + ([.comments[].body] | map("- " + .) | join("\n")) + "\n" else "" end)' \
      >> "$ISSUE_CONTEXT_FILE"
  done <<< "$ISSUE_NUMBERS"
else
  printf '%s\n' '<no linked issues found — derive requirements from the PR description only>' > "$ISSUE_CONTEXT_FILE"
fi
```

6. **INPUT 2 — implementation plan + Implementation Report.** Resolution order for the plan: `--plan=<path>` argument → `./.pair/PLAN.md` in the current worktree → none. For the report: `--report=<path>` → `./.pair/REPORT.md` → the most recent PR comment starting with `## Implementation Report` → none. Both are optional; the prompt states explicitly which one is missing so Codex does not guess.

```bash
PLAN_ARG=$(printf '%s' "$ARGUMENTS" | grep -o -- '--plan=[^ ]*' | cut -d= -f2-)
REPORT_ARG=$(printf '%s' "$ARGUMENTS" | grep -o -- '--report=[^ ]*' | cut -d= -f2-)
PLAN_FILE="${PLAN_ARG:-.pair/PLAN.md}"
REPORT_FILE="${REPORT_ARG:-.pair/REPORT.md}"

PLAN_CONTEXT_FILE=$(mktemp -t nuclear-plan-context-XXXX.md)
if [ -s "$PLAN_FILE" ]; then
  { printf '## Implementation Plan (%s)\n\n' "$PLAN_FILE"; cat "$PLAN_FILE"; printf '\n'; } > "$PLAN_CONTEXT_FILE"
  HAS_PLAN=1
else
  printf '%s\n' '<no implementation plan available — GATE 3 (architecture) is N/A; review against requirements only>' > "$PLAN_CONTEXT_FILE"
  HAS_PLAN=0
fi

REPORT_CONTEXT_FILE=$(mktemp -t nuclear-report-context-XXXX.md)
if [ -s "$REPORT_FILE" ]; then
  cat "$REPORT_FILE" > "$REPORT_CONTEXT_FILE"
else
  gh pr view "$PR_NUMBER" --json comments \
    --jq '[.comments[] | select(.body | startswith("## Implementation Report"))] | last | .body // empty' \
    > "$REPORT_CONTEXT_FILE"
fi
[ -s "$REPORT_CONTEXT_FILE" ] || printf '%s\n' '<no Implementation Report — no plan-adherence verification ran for this PR>' > "$REPORT_CONTEXT_FILE"
```

7. Initialize the **iteration history log** — a running record you maintain in memory throughout the loop. Start empty:

```
ITERATION_HISTORY = ""
```

## Phase 1: Run Codex Review

Build the Codex review prompt. It includes the three inputs, the four gates, the defect checklist, the severity scale, the mandatory finding format, and (from iteration 2) the iteration history.

**IMPORTANT — command form.** We need a custom prompt, and `codex exec review --base <branch>` is **mutually exclusive with a custom PROMPT** — one of several reasons the `review` subcommand is not used here (see below). We run `codex exec` and instruct Codex in-prompt to diff `HEAD` against `origin/$BASE_BRANCH`.

**IMPORTANT — flags (verified on codex-cli 0.136.0).** Use `codex exec - -s workspace-write --ephemeral --json` with a writable `TMPDIR`. Do **not** use `codex exec review`: it accepts NEITHER `-s/--sandbox` NOR `-a/--ask-for-approval` NOR `--full-auto` (passing any errors with `unexpected argument '-s' found`), which pins it to read-only forever — and read-only is exactly what breaks the review (see the runner block). `codex exec` also rejects `--title`. Use Codex's default model (no `--model` / no `-c model=...`).

**IMPORTANT — the `review` subcommand is retired here.** Besides being stuck read-only, `codex exec review` hangs **silently**: the process stays alive but emits only `thread.started` + `turn.started` (~2 JSONL events), never runs a command, and never errors — observed freezing 20–34 min on a small diff, while a trivial `codex exec` ping returned fine in the same window (so codex/auth is alive; the stall is specific to the `review` path's model call). It burned ~10 min of watchdog time per iteration on PR #1325 and never once produced output. The in-prompt "diff HEAD vs origin/$BASE_BRANCH" instruction means it adds nothing essential. `codex exec` can itself hang *before* the final consolidated message — so the parser collects **all** `agent_message` events (codex streams substantive findings across intermediate messages), not just the last.

**IMPORTANT — the worst failure mode: `codex exec review` SILENTLY DROPS FINDINGS.** The hang above is at least visible. The subcommand's other mode is worse, because it looks like success. Measured on a real drain (COTIntelligence, 8 branches): on one branch it returned a clean 249-character review *after 7 genuine file reads* — it read the code and reported nothing — while the **same prompt** through `codex exec - -s read-only --ephemeral --json` found two real defects and returned `VERDICT: CHANGES_REQUESTED`. Across all eight branches it never once emitted the VERDICT line. So what is lost is not merely the verdict line to parse; the findings themselves never arrive. **A zero-finding review from `codex exec review` is not evidence that a branch is clean — it is no evidence at all.** If such a review turns up anywhere (a subagent, an older workflow, a pasted transcript), discard it and re-run through plain `codex exec`.

**Prompt template (all iterations) — assemble into `$REVIEW_PROMPT`.** This is THE review prompt for every workflow in this suite (`/issue-pipeline`, `/drain-issues` invoke it by reference). Never substitute a bare word like `review` for it.

```
Review PR #$PR_NUMBER — "$PR_TITLE" against the ORIGINAL REQUIREMENTS and the IMPLEMENTATION PLAN below. This is a NUCLEAR review across four gates: correctness, requirements, architecture, and security, each run through an explicit defect checklist. Approval requires ALL gates to pass.

Do NOT rewrite the code. You review, reproduce, and report. Every correction you propose is applied by a separate coder after triage.

SCOPE
- Diff HEAD against origin/$BASE_BRANCH. Review only files changed in that diff, plus the directly-affected execution paths.
- The PR's stated intent is: $PR_INTENT_ONE_LINE.
- Do NOT request unrelated refactors or cleanup of code the PR did not touch (smells in untouched files are out of scope).
- YOU are the reviewer. Do NOT invoke the `gh-workflow-suite` skill, and do NOT run `scripts/run_review.py` or any other review gateway. Those spawn a nested reviewer subprocess which cannot initialize inside this sandbox (`failed to initialize in-process app-server client: Operation not permitted`), and the gateway then fails closed to `INCONCLUSIVE`/exit 6 with zero findings. Inspect the diff yourself and emit the report in your own final message.
- EXCEPTION — completeness/security carve-out: if satisfying an explicit acceptance criterion, restoring an invariant named in the plan's Side-Effects Trace, or closing a realistic security hole genuinely requires touching a non-diff file, you MAY raise it. Name the file and explain why changed files alone cannot fix it. If the fix is broad/architectural/risky, mark it BLOCKED with a remediation plan rather than waving it through.

=== INPUT 1: ORIGINAL REQUIREMENTS ===

PR:
$(cat "$PR_CONTEXT_FILE")

LINKED ISSUE(S):
$(cat "$ISSUE_CONTEXT_FILE")

=== INPUT 2: IMPLEMENTATION PLAN ===

$(cat "$PLAN_CONTEXT_FILE")

IMPLEMENTATION REPORT (plan-vs-diff adherence, produced by an independent verifier — its 🔀 diverged and ➕ unplanned items are the FIRST things to scrutinize):
$(cat "$REPORT_CONTEXT_FILE")

=== INPUT 3: THE DIFF ===
Run `git diff origin/$BASE_BRANCH...HEAD` yourself and read every hunk. Open surrounding code as needed to trace call sites, invariants, and state.

=== GATE 1: CORRECTNESS === (tag [CORRECTNESS])
- Does each changed path do what the requirements and plan say, on realistic inputs?
- Trace every modified function to its callers. What assumption of a caller not in the diff now breaks?
- If a CORRECTNESS finding requires stacking 3+ unlikely conditions to manifest, it is at most MEDIUM. (This downgrade does NOT apply to [AC], [ARCH], or [SECURITY] findings.)

=== GATE 2: REQUIREMENTS === (tag [AC])
- Extract acceptance criteria from the linked issue bodies/comments AND the PR description. Prefer explicit sections named "Acceptance Criteria", "AC", "Requirements", "Done When", "Definition of Done", or checklist items. If a plan exists, its Acceptance Criteria section is also a source.
- If no explicit AC exists, infer only concrete, user-visible requirements actually stated by the issue/PR. Mark those as "inferred". Do NOT invent requirements.
- Produce this matrix:

## AC Coverage Matrix
| Criterion | Source | Status | Evidence | Severity |
|---|---|---|---|---|
| <criterion> | Issue #N / PR body / PLAN.md / inferred | Covered / Partial / Missing / N/A | file:line, test, or behavior | none / BLOCKER[AC] / HIGH[AC] / MEDIUM[AC] |

AC severity rules:
- An explicit criterion that is MISSING = BLOCKER[AC]. PARTIAL = HIGH[AC]. Both block — LGTM is forbidden while any explicit AC is Missing/Partial without a recorded non-blocking rationale.
- An "inferred" criterion that is ambiguous = MEDIUM[AC] unless the PR explicitly claims to satisfy it.
- Also flag any place where the PR DESCRIPTION claims behavior the diff does not actually implement (description drift) as HIGH[AC].

=== GATE 3: ARCHITECTURE === (tag [ARCH]) — N/A if no implementation plan was provided; say so and skip.
- Compare the diff against the plan's Proposed Fix, Files & Line Numbers, and Side-Effects Trace.
- Start from the Implementation Report's 🔀 diverged and ➕ unplanned items. For each: is the deviation an improvement, neutral, or does it break an invariant/assumption the plan recorded? Only the last is a finding.
- A deviation that changes a public contract, a shared-state invariant, a locking/dedup/cache discipline, or the failure-handling strategy the plan specified = HIGH[ARCH]. Escalate to BLOCKER[ARCH] only if it also causes production breakage or data loss.
- Unplanned changes that touch code outside the plan's Files list without a stated reason = MEDIUM[ARCH] (HIGH if they alter behavior on a common path).
- The plan's "What I Am Most Likely Wrong About" paragraph: did the implementation confirm or refute that weak assumption? Report which.

=== GATE 4: SECURITY === (tag [SECURITY])
Perform an explicit security review of changed files and directly-affected paths. Check at least:
- Injection: SQL, NoSQL, LDAP, OS/shell command, template, header, log injection
- XSS / HTML injection / unsafe rendering / open redirects
- Authn/authz: missing checks, IDOR, tenant isolation, privilege escalation
- Secrets: hardcoded credentials, token/secret leakage, secrets in logs or error messages
- Input validation, output encoding, path traversal, unsafe file upload
- SSRF, unsafe URL fetching, open-proxy behavior
- Unsafe deserialization, prototype pollution, XXE
- CSRF / session / cookie / token handling
- Crypto, randomness, password-hashing misuse
- Race conditions / TOCTOU on security-sensitive operations
- Dependency/lockfile changes introducing known-risky packages

Security severity (exploitability, NOT frequency — a low-likelihood auth bypass is still high severity):
- BLOCKER[SECURITY]: realistic exploit → auth bypass, RCE, secret/data exfiltration, cross-tenant access, destructive action, or major privacy breach.
- HIGH[SECURITY]: realistic exploit with limited blast radius, missing authz on a common path, meaningful validation gap, or sensitive info exposure.
- MEDIUM[SECURITY]: hardening / defense-in-depth / theoretical concern with no concrete exploit path.

=== DEFECT CHECKLIST (apply under every gate; each item needs an explicit yes/no with evidence) ===
- Race conditions: concurrent callers, async ordering, unguarded read-modify-write, retries that duplicate side effects, detached tasks without supervision.
- State inconsistencies: partial updates without rollback, caches/dedup sets/in-memory state that diverge from the source of truth, invariants held on one path but not another (set on write, not checked on read, or vice versa).
- Database issues: missing/irreversible migrations, transaction boundaries, missing indexes on new query patterns, N+1, constraint/uniqueness violations, nullable columns the code assumes non-null, schema/ORM drift.
- Performance regressions: new O(n²) over user-sized collections, unbounded queries, blocking I/O on hot paths, chatty loops over network/DB, memory growth without bound.
- Missing edge cases: empty/null/zero, boundaries, unicode/encoding, timezones, very large inputs, error paths of every new external call, cancellation/timeouts.
- Missing tests: the plan's failing-first test exists and asserts the stated behavior (HIGH if absent); every new branch/edge case above has coverage (MEDIUM if not); regression surface named in the plan still passes. If the sandbox allows, RUN the test suite and report the result.

=== SEVERITY SCALE ===
- BLOCKER: will break production or lose data on a realistic path; realistic exploit with major blast radius; explicit requirement MISSING.
- HIGH: concrete bug on a documented/common path; limited-blast-radius exploit or missing authz on a common path; explicit requirement PARTIAL; plan deviation breaking a contract/invariant; the plan's primary test absent.
- MEDIUM: bug needing uncommon conditions; performance regression without user-visible impact yet; missing edge-case test; hardening.
- LOW: naming, style, minor cleanup.
Only BLOCKER and HIGH block approval. Use them only when the evidence supports them — do not inflate.

=== FINDING FORMAT (mandatory for EVERY finding, all severities) ===
### [<SEVERITY>][<AXIS>] <one-line title>
- File: <path>:<line-range>
- Relevant code:
  ```
  <the exact lines, quoted>
  ```
- Why it is a problem: <mechanism, not adjective — what invariant/requirement/plan item is violated>
- Reproduction scenario: <concrete inputs/state → observed wrong behavior. For [AC]/[ARCH]: the criterion or plan item and the evidence it is unmet.>
- Proposed correction: <specific change — what to do at that site; a short diff sketch if it clarifies. Do NOT apply it.>

ANTI-ESCALATION
- If you already raised a concern about axis X in a prior iteration (see history below) and the author implemented the agreed fix, DO NOT come back with a stricter variant of the same concern. Pick the strictest version you care about on iteration 1 and stick with it. Subsequent rounds should find NEW issues, verify prior fixes, or approve.
- This does NOT apply to [AC], [ARCH], or [SECURITY]: never soften a confirmed criterion gap, invariant break, or vulnerability to end a round.

CONTEXT FROM PREVIOUS ITERATIONS:
${ITERATION_HISTORY:-<none - this is iteration 1>}

INSTRUCTIONS
- Do NOT re-raise issues dismissed or fixed above (except unresolved [AC]/[ARCH]/[SECURITY]).
- Focus on NEW problems or verification of prior fixes.
- If a prior fix was wrong, call out the SPECIFIC remaining bug — don't re-raise the whole axis.

OUTPUT CONTRACT
- Sections, in order: `## Findings` (grouped BLOCKER → HIGH → MEDIUM → LOW, each in the mandatory format), `## AC Coverage Matrix`, `## Architecture Summary` (or "N/A — no plan"), `## Security Summary`, `## Defect Checklist` (one line per checklist item: yes/no + evidence), `## Tests` (what you ran, or why you could not).
- End with EXACTLY one line:
  VERDICT: LGTM
  VERDICT: CHANGES_REQUESTED
  VERDICT: BLOCKED
- Use LGTM only when ALL of these hold:
  * no BLOCKER/HIGH [CORRECTNESS] findings remain,
  * no BLOCKER/HIGH [AC] findings remain (no explicit AC is Missing or Partial without a recorded non-blocking rationale),
  * no BLOCKER/HIGH [ARCH] findings remain (or the gate is N/A),
  * no BLOCKER/HIGH [SECURITY] findings remain.
- Use BLOCKED when a blocking [AC]/[ARCH]/[SECURITY] finding requires a broad/architectural/risky fix that should not be jammed into this PR.
- If the diff is clean across all gates, approve with LGTM — do not invent findings to justify a round.
```

Write the assembled prompt to a file and run via the **watchdog runner** below. It runs `codex exec - -s workspace-write` with an explicit writable `TMPDIR`. Capture stdout and stderr; parse the captured JSONL.

**Why `workspace-write` + `TMPDIR` and not `read-only`** (root-caused on PR #1325, 2026-07-25): under `-s read-only` the sandbox denies writes to *every* temp candidate, so the `gh-workflow-suite` gateway self-test dies before the review starts:

```
run_review.py:1494  tempfile.TemporaryDirectory(prefix="review-self-source-")
FileNotFoundError: [Errno 2] No usable temporary directory found in
['/var/folders/.../T/', '/tmp', '/var/tmp', '/usr/tmp', '<cwd>']
```

That skill is **fail-closed**: no gateway → it may not issue `APPROVE`, so it emits `VERDICT: BLOCKED` even with zero findings. Read-only also means Codex can never execute a test, so every finding is static inference — and the defect checklist's "Missing tests" item explicitly asks Codex to run the suite. `workspace-write` + writable `TMPDIR` fixes both. Verified 2026-07-28: the same self-test that returned exit 1 above returns `{"ok": true, "tests": 28}` exit 0, and the worktree stayed clean (`git status` empty). Separately observed 2026-07-25 on epic #1188: with the write bit Codex executed the real PostgreSQL suite and confirmed fixes by running them instead of by reading. **`workspace-write` does not mean Codex edits your code during review** — the prompt is read-and-report; it needs the write bit for temp files and test runners. After every run, `git status --porcelain` must be empty; if Codex left edits, `git checkout -- . && git clean -fd` them and note it in the iteration history.

```bash
# Write the prompt to a file: avoids ARG_MAX and shell-quoting issues. If you
# assembled $REVIEW_PROMPT with cat/heredoc, make sure the PR/issue/plan bodies
# were appended RAW (never via an unquoted heredoc) or backticks in the bodies
# get command-substituted.
REVIEW_PROMPT_FILE=$(mktemp -t nuclear-review-prompt-XXXX.md)
printf '%s' "$REVIEW_PROMPT" > "$REVIEW_PROMPT_FILE"

CODEX_OUT=$(mktemp -t codex-review-out-XXXX.jsonl)
CODEX_ERR=$(mktemp -t codex-review-err-XXXX.log)

# Writable TMPDIR for the codex child. REQUIRED: the gateway self-test and any
# test runner Codex invokes both need a usable temp dir. If your harness gives
# you a session scratchpad, point this at it instead (verified-good); mktemp -d
# is the portable default.
CODEX_TMPDIR=$(mktemp -d -t nuclear-codex-tmp-XXXX)
mkdir -p "$CODEX_TMPDIR" && [ -w "$CODEX_TMPDIR" ] || {
  echo "FATAL: no writable TMPDIR for codex ($CODEX_TMPDIR)" >&2; return 1 2>/dev/null || exit 1; }

# Watchdog runner. macOS has no `timeout`/`setsid`, so we background codex and
# kill it if its JSONL output stops growing for STALL_SECS while still alive
# (the silent-hang signature), or if it exceeds MAX_SECS overall.
# NOTE: this script sleeps internally — if your harness blocks foreground
# `sleep`, launch this whole block as a background Bash command (run_in_background)
# and read $CODEX_OUT when notified.
run_codex() {
  local label="$1"; shift            # remaining args = codex argv
  : > "$CODEX_OUT"; : > "$CODEX_ERR"
  ( cat "$REVIEW_PROMPT_FILE" | TMPDIR="$CODEX_TMPDIR" "$@" > "$CODEX_OUT" 2> "$CODEX_ERR" ) &
  # STALL_SECS=420: codex legitimately goes >150s between JSONL events during
  # long reasoning — 150 killed healthy runs mid-review (PARTIAL_REVIEW).
  local pid=$! STALL_SECS=420 MAX_SECS=1800 last=0 stalled=0 elapsed=0 now
  while kill -0 "$pid" 2>/dev/null; do
    sleep 15; elapsed=$((elapsed+15))
    now=$(wc -l < "$CODEX_OUT" 2>/dev/null | tr -d ' '); now=${now:-0}
    if [ "$now" -gt "$last" ]; then last="$now"; stalled=0; else stalled=$((stalled+15)); fi
    if [ "$stalled" -ge "$STALL_SECS" ] || [ "$elapsed" -ge "$MAX_SECS" ]; then
      echo "[$label] watchdog kill: stalled=${stalled}s elapsed=${elapsed}s last=${last} events" >&2
      kill "$pid" 2>/dev/null; pkill -P "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
      return 124
    fi
  done
  wait "$pid" 2>/dev/null; return 0
}

# Parse ALL agent_message events. Codex streams substantive findings across
# intermediate messages and may hang before the final consolidated one; prefer
# the last message carrying a VERDICT line or the AC matrix, else emit every
# streamed message tagged PARTIAL_REVIEW so a hang-before-summary still surfaces
# the findings.
parse_review() {
  python3 - "$CODEX_OUT" <<'PY'
import json, sys
msgs = []
with open(sys.argv[1]) as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try: event = json.loads(line)
        except Exception: continue
        if event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message" and item.get("text"):
                msgs.append(item["text"])
if not msgs:
    print("NO_REVIEW_OUTPUT"); sys.exit()
final = [m for m in msgs if "VERDICT:" in m or "AC Coverage Matrix" in m]
if final:
    print(final[-1])
else:
    print("PARTIAL_REVIEW (no VERDICT line — codex likely hung before final summary):\n\n"
          + "\n\n---\n\n".join(msgs))
PY
}

# Single path: `codex exec` with a writable sandbox. The `codex exec review`
# subcommand is deliberately NOT used — it rejects `-s`, so it is permanently
# stuck read-only and can never satisfy the gateway self-test or run a test.
# It has also hung silently on every observed run and silently dropped findings.
# The in-prompt "diff HEAD vs origin/$BASE_BRANCH" instruction makes `exec` a
# faithful substitute.
run_codex "exec-workspace-write" codex exec - -s workspace-write --ephemeral --json
REVIEW_TEXT=$(parse_review)

# One retry on empty/partial output (transient model or network stall).
if [ "$REVIEW_TEXT" = "NO_REVIEW_OUTPUT" ] || printf '%s' "$REVIEW_TEXT" | grep -q '^PARTIAL_REVIEW'; then
  echo "codex exec produced empty/partial output — retrying once" >&2
  run_codex "exec-workspace-write-retry" codex exec - -s workspace-write --ephemeral --json
  REVIEW_TEXT=$(parse_review)
fi

# Surface a gateway-preflight failure explicitly: it is NOT a code finding, but
# it does mean the review ran degraded (no test execution). See Phase 2.
if grep -qi 'No usable temporary directory\|gateway.*\(self-test\|preflight\)' "$CODEX_ERR" "$CODEX_OUT" 2>/dev/null; then
  echo "WARNING: gateway preflight failed despite writable TMPDIR ($CODEX_TMPDIR) — treat any BLOCKED verdict as infra, not code" >&2
fi

# Codex must not have edited the worktree. If it did, revert and record it.
if [ -n "$(git status --porcelain)" ]; then
  echo "WARNING: codex left worktree edits during review — reverting" >&2
  git status --porcelain >&2
  git checkout -- . && git clean -fd
fi

echo "$REVIEW_TEXT"
```

(Note: `--base` is deliberately omitted. Codex infers the diff from the in-prompt instruction to diff `HEAD` vs `origin/$BASE_BRANCH`.)

This may take 3-10 minutes (longer if the retry fires). That is normal — Codex is doing a thorough, file-aware review, and with `workspace-write` it may also be running the test suite.

## Phase 2: Parse the Review

Read the final `VERDICT:` line — it is authoritative. Then cross-check against the findings:

1. **`VERDICT: LGTM`** (and no `BLOCKER`/`HIGH` findings of any axis remain) → Codex gate passed, go to **Phase 3.5 (CI gate)**.
2. **`VERDICT: CHANGES_REQUESTED`**, or any `BLOCKER`/`HIGH` finding (`[CORRECTNESS]`, `[AC]`, `[ARCH]`, `[SECURITY]`) present → proceed to Phase 3.
3. **`VERDICT: BLOCKED`** → **count the findings before acting; the verdict line alone is not the signal.** There are three distinct causes:
   - **Real block** — a BLOCKER/HIGH `[AC]`/`[ARCH]`/`[SECURITY]` finding needs a fix too big for this PR. Do NOT approve. Open a follow-up issue with the remediation plan, report the PR as **incomplete/blocked**, and stop the loop.
   - **Degraded** — the gateway failed but Codex inspected the diff anyway and returned real findings (look for "findings come from manual committed-diff inspection"). **The findings are valid** — treat them on their merits and continue the loop.
   - **Infra abort** — zero findings, and the prose blames Codex's own tooling ("Gateway self-check failed"; "Blocker is review infrastructure, not requested code changes"). This is a **failed** review, not an approval and not a code block. Re-run with a writable `TMPDIR` and `-s workspace-write`. If it still aborts, say the review is **inconclusive** — never approve on it, and never present your own test runs as if they were the independent gate.
4. **`NO_REVIEW_OUTPUT`** → DO NOT treat as approved. Empty output means Codex failed to run AND the retry also produced nothing. Inspect `$CODEX_ERR` for the cause (auth, rate-limit, ARG_MAX, prompt-too-large, network). The runner already retried once; do not loop indefinitely. If still empty, abort the loop and report status `inconclusive` with the stderr head — never auto-merge on inconclusive review.
5. **`PARTIAL_REVIEW …`** (prefix) → Codex did real analysis but hung before the final consolidated VERDICT/AC-matrix message. The streamed `agent_message`s are included — read them. If they contain substantive across-the-gates findings with **no `BLOCKER`/`HIGH`** (Codex's text explicitly confirming the fix/clean gates), treat it like a clean run for gating purposes but, per the verdict-line-unreliability rule, **require two such consecutive BLOCKER/HIGH-free runs** before passing to the CI gate. If any `BLOCKER`/`HIGH` is present in the streamed text, proceed to Phase 3. Do NOT approve on a single partial with no corroboration.
6. **Format drift** — if a finding is missing any of the five mandatory fields (file, relevant code, why, reproduction, proposed correction), do not discard it. Triage it on its merits, and in the next iteration's history note `<finding> — incomplete format; supply reproduction + correction`. A finding without a reproduction scenario cannot be `BLOCKER`; cap it at `HIGH` until Codex supplies one (this cap does NOT apply to `[AC]`/`[SECURITY]`).

Print the full review text (including the AC Coverage Matrix and Defect Checklist) so the user can see what Codex found.

## Phase 3: Fix Issues

For each `BLOCKER` and `HIGH` finding, first classify it:

- **`[AC]`, `[ARCH]`, or `[SECURITY]` finding** → the carve-out applies. Fix it even if it touches a non-diff file, **provided** the fix is minimal and direct. If the fix is broad/architectural/risky → escalate to **BLOCKED** (follow-up issue), do not jam it in. **Never** dismiss an `[AC]`/`[ARCH]`/`[SECURITY]` finding as scope expansion or same-axis escalation.
  - `[ARCH]` nuance: if the deviation is a *better* path than the plan and breaks no recorded invariant, the fix is to **record the deviation** (PR body + iteration history, and update `.pair/PLAN.md` if it exists) rather than to revert the code. Codex's finding must have named a broken invariant to require a code change.
- **Same-axis escalation** (correctness concern from a prior iteration, now a stricter variant of an already-agreed fix): **dismiss** per the Anti-Escalation Rule. Record in history. Do not fix.
- **Scope expansion** of a *correctness/smell* finding (fix requires editing untouched files or restructuring unrelated code): **dismiss** as out-of-scope. Record in history with a one-line follow-up note. Do not fix.
- **Genuine new correctness bug on a realistic path**: fix it.

`MEDIUM`/`LOW` findings: do not fix in-loop unless the fix is a one-liner inside a file you are already editing for a blocking finding. Otherwise record as `NOTED`; open a single follow-up issue for the MEDIUMs at the end.

For each issue you decide to fix:

1. Read the file mentioned in the review — the finding's "Relevant code" block tells you the exact site
2. Understand the issue Codex described; verify the reproduction scenario is real
3. Start from the "Proposed correction", but fix the root cause — do not apply a correction you can see is wrong just because it was proposed
4. Make any in-scope companion changes (tests, config). Every `[CORRECTNESS]` fix with a reproduction scenario gets a test that encodes that scenario. If the fix sprawls beyond the carve-out, reclassify as BLOCKED.

After all fixes:

```bash
git add -A
git commit -m "fix: address Codex nuclear review feedback (iteration N)

Fixed issues:
- [BLOCKER][CORRECTNESS] description...
- [HIGH][AC] description...
- [HIGH][ARCH] description...
- [HIGH][SECURITY] description...

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
git push
```

### Update Iteration History

After each iteration (fixed or dismissed), append a summary to `ITERATION_HISTORY`. This is passed to Codex next round.

For each issue Codex raised, record ONE outcome:

```
Iteration N:
- [BLOCKER][CORRECTNESS] <issue> → FIXED: <what you changed, file:line>
- [HIGH][AC] <criterion> → FIXED: <what you changed, file:line>
- [HIGH][ARCH] <deviation> → FIXED: <reverted to plan / invariant restored> | RECORDED: <better path, no invariant broken; plan updated>
- [HIGH][SECURITY] <vuln> → FIXED: <what you changed>
- [HIGH][CORRECTNESS] <issue> → DISMISSED (escalation): same axis as iter K's <finding>; stricter variant of already-fixed concern
- [HIGH][CORRECTNESS] <issue> → DISMISSED (out-of-scope): would require editing <file> the PR does not touch; follow-up issue
- [BLOCKER][AC] <criterion> → BLOCKED: needs <broad change>; follow-up issue #M opened; PR incomplete
- [MEDIUM][...] <issue> → NOTED (non-blocking; follow-up)
- [LOW][...] <issue> → NOTED
```

Example:
```
Iteration 3:
- [BLOCKER][SECURITY] SQL injection in user_logs query → FIXED: parameterized the query in services.py:45
- [HIGH][AC] "export must include archived rows" missing → FIXED: added archived filter toggle in export.py:88 + test_export_archived
- [HIGH][ARCH] dedup set checked on read but plan required check on write too → FIXED: guard added in ingest.py:120
- [HIGH][SECURITY] IDOR — report endpoint lacks ownership check → FIXED: added org-scope guard in views.py:120
- [MEDIUM][CORRECTNESS] Missing index on user_id column → NOTED (follow-up #212)
- [LOW] Consider adding type hints to helpers → NOTED
```

Then **go back to Phase 1** with the next iteration number.

## Phase 3.5: CI Gate

Runs only after Codex returns LGTM (Phase 2 case 1, or two corroborating PARTIAL_REVIEWs). Skipped only with `--no-ci-gate`, and then the final result is `APPROVED_NO_CI`.

**Rule (from global CLAUDE.md, restated so it cannot be waived in-loop):** a required check that did not run is missing evidence, not a pass. A billing, quota, runner, or provider failure explains why it did not run; it never satisfies it. Local test runs — yours or Codex's — do not substitute for the required status context.

```bash
# Wait for checks on the PR head to settle. gh has no non-interactive
# "wait for checks" that also reports missing contexts, so poll. NOTE: sleeps
# internally — run as a background Bash command if the harness blocks
# foreground sleep, same as the codex watchdog.
HEAD_SHA=$(git rev-parse HEAD)
CI_MAX_SECS=1800; CI_ELAPSED=0
while :; do
  CHECKS_JSON=$(gh pr checks "$PR_NUMBER" --json name,bucket,state,link 2>/dev/null || echo '[]')
  PENDING=$(printf '%s' "$CHECKS_JSON" | python3 -c 'import json,sys; print(sum(1 for c in json.load(sys.stdin) if c.get("bucket")=="pending"))')
  [ "$PENDING" = "0" ] && break
  [ "$CI_ELAPSED" -ge "$CI_MAX_SECS" ] && { echo "CI still pending after ${CI_MAX_SECS}s" >&2; break; }
  sleep 30; CI_ELAPSED=$((CI_ELAPSED+30))
done

# Required contexts from branch protection (404 = no protection → every reported check is treated as required).
REQUIRED=$(gh api "repos/{owner}/{repo}/branches/$BASE_BRANCH/protection/required_status_checks" --jq '.contexts[]' 2>/dev/null || true)

CI_RESULT=$(printf '%s' "$CHECKS_JSON" | REQUIRED="$REQUIRED" python3 -c '
import json, os, sys
checks = json.load(sys.stdin)
required = [r for r in os.environ.get("REQUIRED","").splitlines() if r.strip()]
by_name = {c["name"]: c for c in checks}
if required:
    missing = [r for r in required if r not in by_name]
    fails   = [r for r in required if r in by_name and by_name[r]["bucket"] in ("fail","cancel")]
    pending = [r for r in required if r in by_name and by_name[r]["bucket"]=="pending"]
else:
    missing = [] if checks else ["<no checks reported on this PR>"]
    fails   = [c["name"] for c in checks if c["bucket"] in ("fail","cancel")]
    pending = [c["name"] for c in checks if c["bucket"]=="pending"]
if fails:     print("CI_FAILED: " + ", ".join(fails))
elif missing or pending: print("CI_MISSING: " + ", ".join(missing+pending))
else:         print("CI_GREEN")
')
echo "$CI_RESULT"
```

Act on the result:

- **`CI_GREEN`** → **APPROVED**. Go to Phase 4.
- **`CI_FAILED: <checks>`** → pull the failing job log (`gh run view <run-id> --log-failed`, run id from the check's `link`), turn each distinct failure into a `[BLOCKER][CI]` finding in the iteration history (file, failing test/step, error text, proposed fix), and go back to **Phase 3** to fix it. That counts as an iteration. After the fix is pushed, re-run **Phase 1** (Codex must re-review the CI fix — a CI fix is code), then this gate again. If the failure is unrelated to the PR (flaky test, base-branch breakage confirmed by the same failure on `origin/$BASE_BRANCH`), say so with the evidence, re-run the check once (`gh run rerun <run-id> --failed`), and if it still fails report `CI_FAILED` — do not approve.
- **`CI_MISSING: <checks>`** → the PR is **not approved**. Report `CI_MISSING` with the list of contexts that never reported and the reason if visible (`gh run list --branch <head> --limit 5` — a run that never started, a billing/quota message, a runner error). Do not retry indefinitely; do not touch branch protection; do not present your own or Codex's test run as a substitute. The user must restore CI and rerun the same SHA.

## Phase 4: Approved — Summary

When Codex LGTM + CI green (or max iterations reached / BLOCKED / CI_FAILED / CI_MISSING), print:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ PR #$PR_NUMBER — Nuclear Review Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iterations: N
Result: APPROVED / APPROVED_NO_CI / MAX_ITERATIONS_REACHED / BLOCKED / CI_FAILED / CI_MISSING / INCONCLUSIVE

Inputs:
- Requirements: Issue #N (+ PR body) / PR body only
- Plan:         <path> / none (architecture gate N/A)
- Report:       <path or PR comment> / none

Gate status:
- Correctness:  PASS / N open
- Requirements: PASS (all explicit criteria covered) / N missing-or-partial
- Architecture: PASS / N/A / N deviations breaking invariants
- Security:     PASS / N open
- CI:           GREEN (<checks>) / FAILED (<checks>) / MISSING (<contexts>) / SKIPPED (--no-ci-gate)

AC Coverage Matrix (final):
[paste the matrix from the last review]

Defect Checklist (final):
[paste the checklist lines from the last review]

Review History:
- Iteration 1: [summary]
- Iteration 2: [summary]
...

Fixes Applied:
- [list of commits pushed]

Follow-up issues opened (if any):
- #M: [blocked item + remediation plan]
- #K: [MEDIUM findings batched]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Important Notes

- **Three inputs, every iteration:** requirements (issue + PR body), plan (`.pair/PLAN.md` + Implementation Report, when they exist), diff. A review that ran without the plan when one existed is a degraded review — say so in the summary. `/issue-pipeline` and `/drain-issues` always pass `--plan=<worktree>/.pair/PLAN.md --report=<worktree>/.pair/REPORT.md`.
- **Sandbox:** run `codex exec - -s workspace-write --ephemeral --json` with `TMPDIR` pointed at a writable dir. Read-only starves the `gh-workflow-suite` gateway self-test (`tempfile.TemporaryDirectory` → `No usable temporary directory found`), which makes it fail closed to `BLOCKED`, and it also prevents Codex from running a single test. `workspace-write` is for temp files and test runners, not code edits — the worktree stayed clean across verified runs, and the runner now reverts anything Codex leaves behind.
- **`codex exec review` is retired:** it rejects `-s`/`-a`/`--full-auto` (so it can never leave read-only), hangs silently after `turn.started`, and — when it *does* answer — drops findings: a measured clean 249-char review after 7 real file reads, where plain `codex exec` on the same prompt found two real defects and returned `CHANGES_REQUESTED`. **A zero-finding review from it is not evidence of a clean branch.** Use `codex exec` only. Parse **all** `agent_message` events, not just the last — `exec` may hang before the final summary, and its intermediate messages carry the real findings (`PARTIAL_REVIEW`). (macOS lacks `timeout`/`setsid` — the watchdog backgrounds codex and kills on stall instead. STALL_SECS=420 / MAX_SECS=1800; 150s killed healthy runs mid-review.)
- **Severity:** `BLOCKER` / `HIGH` / `MEDIUM` / `LOW`, each tagged `[CORRECTNESS]` / `[AC]` / `[ARCH]` / `[SECURITY]` / `[CI]`. Only BLOCKER and HIGH block approval. Legacy `[P1]`/`[P2]`/`[P3]` in older transcripts map to BLOCKER / HIGH / MEDIUM.
- **Finding format is mandatory:** file, relevant code, why, reproduction scenario, proposed correction. Codex proposes; the coder applies after the reviewer's triage. Codex never edits.
- The iteration history is passed to Codex each round so it knows what was fixed/dismissed. If Codex re-raises a *correctness* issue already dismissed, skip it. **Never** skip a re-raised `[AC]`/`[ARCH]`/`[SECURITY]` issue on those grounds.
- Approval is gated on the `VERDICT:` line AND zero open BLOCKER/HIGH across all gates AND green required CI — not on fuzzy phrases like "looks good", and never on local test runs standing in for CI.
- Preserve the PR's original intent. The only sanctioned scope expansion is the minimal direct fix for an explicit AC gap, a broken plan invariant, or a real security hole.
