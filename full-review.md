---
allowed-tools: Bash, Read, Edit, Write, Grep, Glob, Agent
argument-hint: <pr-number>
description: Nuclear Claude ↔ Codex review loop - reviews code correctness, acceptance-criteria alignment, and security; Codex reviews, Claude fixes, repeat until approved or max iterations
---

# Full Review: Nuclear Claude ↔ Codex Review Loop

You are entering an automated **nuclear review-fix loop** for **PR #$ARGUMENTS**.

"Nuclear" means the review is not code-diff-only. It evaluates the PR across **three gates**, and approval requires **all three** to pass:

1. **Correctness** — does the changed code work, on realistic paths?
2. **AC alignment** — does the PR actually deliver the acceptance criteria of its linked issue(s) and its own stated description?
3. **Security** — does the diff introduce or leave open a realistic vulnerability?

## Rules

- **MAX_ITERATIONS = 6** — stop after 6 rounds regardless. If unresolved after 6, remaining items go to a follow-up PR; do not keep looping.
- Each iteration: Codex reviews → if changes requested → you fix & push → Codex reviews again
- **Default scope:** prefer fixes limited to files already changed by the PR. Do NOT request or perform unrelated refactors or broad architectural cleanup.
- After fixing, commit and push to the PR branch (do NOT create a new PR)
- When all three gates pass, print a summary and stop

## Completeness / Security Carve-Out (overrides default scope)

The default "stay inside the diff" rule is for *code smells*. It does **not** apply to acceptance-criteria gaps or security holes:

- A finding is **NOT** dismissible as "scope expansion" if it is `[P1]`/`[P2]` and tagged `[AC]` or `[SECURITY]`.
- You MAY touch non-diff files **only** when the change is the *minimal direct fix* required to (a) satisfy an explicit linked-issue acceptance criterion, or (b) close a realistic security hole on the PR's affected path. Codex must name the non-diff file and explain why the changed files alone cannot fix it.
- If the required fix is broad, architectural, or risky → do **not** approve and do **not** hack it in. Mark the PR **BLOCKED**, open a follow-up issue with a concrete remediation plan, and report the PR as **incomplete** — never silently approve a half-delivered feature.

## Anti-Escalation Rule

Codex reviews tend to drift: if it raised "X is too weak" in iteration N and you fixed with a stricter X', it will often come back in iteration N+1 with "X' is still too weak, need X''". This is **same-axis escalation** and you should resist it.

When Codex raises a finding on an axis it already raised in a previous iteration:
- If the prior fix was implemented as agreed: **dismiss** the new finding as same-axis escalation. Record in history.
- Only re-engage if Codex points to a concrete, *different* failure mode (not a stricter hypothetical variant of the same concern).

**Exemption:** anti-escalation and the "needs 3+ stacking conditions → P3" downgrade do **NOT** apply to `[AC]` or `[SECURITY]` findings. A confirmed unmet acceptance criterion or a confirmed vulnerability stays blocking regardless of likelihood — never downgrade it to make a round end.

## Phase 0: Setup & Context Gathering

1. Get PR info and checkout the branch:

```bash
gh pr view $ARGUMENTS --json title,body,headRefName,baseRefName,files
gh pr checkout $ARGUMENTS
```

2. Determine and fetch the base branch:

```bash
BASE_BRANCH=$(gh pr view $ARGUMENTS --json baseRefName -q '.baseRefName')
git fetch origin "$BASE_BRANCH"
echo "Base branch: origin/$BASE_BRANCH"
```

3. Capture the PR's title and stated intent — these get baked into the review prompt so Codex can't drift into out-of-scope refactor requests:

```bash
PR_TITLE=$(gh pr view $ARGUMENTS --json title -q '.title')
PR_INTENT_ONE_LINE=$(gh pr view $ARGUMENTS --json body -q '.body' | awk '/^## Summary/{flag=1;next} /^## /{flag=0} flag && NF' | head -1)
PR_INTENT_ONE_LINE="${PR_INTENT_ONE_LINE:-$PR_TITLE}"
```

4. **Build the PR context block** (full description + changed files) into a file:

```bash
PR_CONTEXT_FILE=$(mktemp -t nuclear-pr-context-XXXX.md)
gh pr view "$ARGUMENTS" \
  --json number,title,url,body,baseRefName,headRefName,files \
  --jq '"# PR #\(.number): \(.title)\nURL: \(.url)\nBase: \(.baseRefName)\nHead: \(.headRefName)\n\n## PR Description\n\(.body // "<empty>")\n\n## Changed Files\n\([.files[].path] | join("\n"))"' \
  > "$PR_CONTEXT_FILE"
```

5. **Resolve linked issue(s) and pull their acceptance criteria.** `closingIssuesReferences` is the primary source; the body regex is a fallback that also catches `Fixes #N` / `Resolves #N`:

```bash
ISSUE_CONTEXT_FILE=$(mktemp -t nuclear-issue-context-XXXX.md)
ISSUE_NUMBERS=$(
  {
    gh pr view "$ARGUMENTS" --json closingIssuesReferences \
      --jq '.closingIssuesReferences[]?.number'
    gh pr view "$ARGUMENTS" --json body --jq '.body // ""' |
      perl -0777 -ne 'while (/(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#([0-9]+)/ig) { print "$1\n" }'
  } | awk 'NF && !seen[$0]++'
)

: > "$ISSUE_CONTEXT_FILE"
if [ -n "$ISSUE_NUMBERS" ]; then
  while IFS= read -r ISSUE_NUMBER; do
    gh issue view "$ISSUE_NUMBER" \
      --json number,title,url,state,labels,body \
      --jq '"## Issue #\(.number): \(.title)\nURL: \(.url)\nState: \(.state)\nLabels: \([.labels[].name] | join(", "))\n\n\(.body // "<empty>")\n"' \
      >> "$ISSUE_CONTEXT_FILE"
  done <<< "$ISSUE_NUMBERS"
else
  printf '%s\n' '<no linked issues found — derive AC from the PR description only>' > "$ISSUE_CONTEXT_FILE"
fi
```

6. Initialize the **iteration history log** — a running record you maintain in memory throughout the loop. Start empty:

```
ITERATION_HISTORY = ""
```

## Phase 1: Run Codex Review

Build the Codex review prompt. It includes the three review gates, scope guardrails, the PR + issue context, and (from iteration 2) the iteration history.

**IMPORTANT — command form.** We need a custom prompt, and `codex exec review --base <branch>` is **mutually exclusive with a custom PROMPT** — one of several reasons the `review` subcommand is not used here (see below). We run `codex exec` and instruct Codex in-prompt to diff `HEAD` against `origin/$BASE_BRANCH`.

**IMPORTANT — flags (verified on codex-cli 0.136.0).** Use `codex exec - -s workspace-write --ephemeral --json` with a writable `TMPDIR`. Do **not** use `codex exec review`: it accepts NEITHER `-s/--sandbox` NOR `-a/--ask-for-approval` NOR `--full-auto` (passing any errors with `unexpected argument '-s' found`), which pins it to read-only forever — and read-only is exactly what breaks the review (see the runner block). `codex exec` also rejects `--title`. Use Codex's default model (no `--model` / no `-c model=...`).

**IMPORTANT — the `review` subcommand is retired here.** Besides being stuck read-only, `codex exec review` hangs **silently**: the process stays alive but emits only `thread.started` + `turn.started` (~2 JSONL events), never runs a command, and never errors — observed freezing 20–34 min on a small diff, while a trivial `codex exec` ping returned fine in the same window (so codex/auth is alive; the stall is specific to the `review` path's model call). It burned ~10 min of watchdog time per iteration on PR #1325 and never once produced output. The in-prompt "diff HEAD vs origin/$BASE_BRANCH" instruction means it adds nothing essential. `codex exec` can itself hang *before* the final consolidated message — so the parser collects **all** `agent_message` events (codex streams substantive findings across intermediate messages), not just the last.

**Prompt template (all iterations) — assemble into `$REVIEW_PROMPT`:**

```
Review PR #$ARGUMENTS — "$PR_TITLE". This is a NUCLEAR review across three gates: correctness, acceptance-criteria alignment, and security. Approval requires ALL THREE to pass.

SCOPE
- Diff HEAD against origin/$BASE_BRANCH. Review only files changed in that diff, plus the directly-affected execution paths.
- The PR's stated intent is: $PR_INTENT_ONE_LINE.
- Do NOT request unrelated refactors or cleanup of code the PR did not touch (smells in untouched files are out of scope).
- YOU are the reviewer. Do NOT invoke the `gh-workflow-suite` skill, and do NOT run `scripts/run_review.py` or any other review gateway. Those spawn a nested reviewer subprocess which cannot initialize inside this sandbox (`failed to initialize in-process app-server client: Operation not permitted`), and the gateway then fails closed to `INCONCLUSIVE`/exit 6 with zero findings. Inspect the diff yourself and emit the report in your own final message.
- EXCEPTION — completeness/security carve-out: if satisfying an explicit acceptance criterion or closing a realistic security hole genuinely requires touching a non-diff file, you MAY raise it. Name the file and explain why changed files alone cannot fix it. If the fix is broad/architectural/risky, mark it BLOCKED with a remediation plan rather than waving it through.

PR AND ISSUE CONTEXT

PR:
$(cat "$PR_CONTEXT_FILE")

LINKED ISSUE(S):
$(cat "$ISSUE_CONTEXT_FILE")

=== GATE 1: CORRECTNESS ===
SEVERITY DISCIPLINE
- [P1] = this WILL break production or cause data loss on a realistic path. Not "could, in theory, if X and Y and Z".
- [P2] = concrete correctness bug on a documented/common path. Not hypothetical edge cases.
- [P3] = suggestions (non-blocking).
- If a CORRECTNESS finding requires stacking 3+ unlikely conditions to manifest, it is at most P3. (This downgrade does NOT apply to [AC] or [SECURITY] findings.)

=== GATE 2: ACCEPTANCE-CRITERIA ALIGNMENT ===
- Extract acceptance criteria from the linked issue bodies AND the PR description. Prefer explicit sections named "Acceptance Criteria", "AC", "Requirements", "Done When", "Definition of Done", or checklist items.
- If no explicit AC exists, infer only concrete, user-visible requirements actually stated by the issue/PR. Mark those as "inferred". Do NOT invent requirements.
- Produce this matrix:

## AC Coverage Matrix
| Criterion | Source | Status | Evidence | Severity |
|---|---|---|---|---|
| <criterion> | Issue #N / PR body / inferred | Covered / Partial / Missing / N/A | file:line, test, or behavior | none / [P1][AC] / [P2][AC] / [P3][AC] |

AC severity rules:
- An explicit criterion that is MISSING or PARTIAL such that the PR does not deliver the linked issue = [P2][AC]. (This is blocking — LGTM is forbidden while any explicit AC is Missing/Partial without a recorded non-blocking rationale.)
- Escalate to [P1][AC] ONLY if the unmet/partial criterion also causes production breakage, data loss, or a serious security/privacy failure.
- An "inferred" criterion that is ambiguous = [P3][AC] unless the PR explicitly claims to satisfy it.
- Also flag any place where the PR DESCRIPTION claims behavior the diff does not actually implement (description drift) as [P2][AC].

=== GATE 3: SECURITY ===
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
- [P1][SECURITY]: realistic exploit → auth bypass, RCE, secret/data exfiltration, cross-tenant access, destructive action, or major privacy breach.
- [P2][SECURITY]: realistic exploit with limited blast radius, missing authz on a common path, meaningful validation gap, or sensitive info exposure.
- [P3][SECURITY]: hardening / defense-in-depth / theoretical concern with no concrete exploit path.

ANTI-ESCALATION
- If you already raised a concern about axis X in a prior iteration (see history below) and the author implemented the agreed fix, DO NOT come back with a stricter variant of the same concern. Pick the strictest version you care about on iteration 1 and stick with it. Subsequent rounds should find NEW issues, verify prior fixes, or approve.
- This does NOT apply to [AC] or [SECURITY]: never soften a confirmed criterion gap or vulnerability to end a round.

CONTEXT FROM PREVIOUS ITERATIONS:
${ITERATION_HISTORY:-<none - this is iteration 1>}

INSTRUCTIONS
- Do NOT re-raise issues dismissed or fixed above (except unresolved [AC]/[SECURITY]).
- Focus on NEW problems or verification of prior fixes.
- If a prior fix was wrong, call out the SPECIFIC remaining bug — don't re-raise the whole axis.

OUTPUT CONTRACT
- Always include the AC Coverage Matrix and a short Security Summary.
- End with EXACTLY one line:
  VERDICT: LGTM
  VERDICT: CHANGES_REQUESTED
  VERDICT: BLOCKED
- Use LGTM only when ALL of these hold:
  * no [P1]/[P2] correctness findings remain,
  * no [P1]/[P2][AC] findings remain (no explicit AC is Missing or Partial without a recorded non-blocking rationale),
  * no [P1]/[P2][SECURITY] findings remain.
- Use BLOCKED when a blocking [AC]/[SECURITY] finding requires a broad/architectural/risky fix that should not be jammed into this PR.
- If the diff is clean across all three gates, approve with LGTM — do not invent findings to justify a round.
```

Write the assembled prompt to a file and run via the **watchdog runner** below. It runs `codex exec - -s workspace-write` with an explicit writable `TMPDIR`. Capture stdout and stderr; parse the captured JSONL.

**Why `workspace-write` + `TMPDIR` and not `read-only`** (root-caused on PR #1325, 2026-07-25): under `-s read-only` the sandbox denies writes to *every* temp candidate, so the `gh-workflow-suite` gateway self-test dies before the review starts:

```
run_review.py:1494  tempfile.TemporaryDirectory(prefix="review-self-source-")
FileNotFoundError: [Errno 2] No usable temporary directory found in
['/var/folders/.../T/', '/tmp', '/var/tmp', '/usr/tmp', '<cwd>']
```

That skill is **fail-closed**: no gateway → it may not issue `APPROVE`, so it emits `VERDICT: BLOCKED` even with zero findings. Read-only also means Codex can never execute a test, so every finding is static inference. `workspace-write` + writable `TMPDIR` fixes both. Verified 2026-07-28: the same self-test that returned exit 1 above returns `{"ok": true, "tests": 28}` exit 0, and the worktree stayed clean (`git status` empty). Separately observed 2026-07-25 on epic #1188: with the write bit Codex executed the real PostgreSQL suite and confirmed fixes by running them instead of by reading. **`workspace-write` does not mean Codex edits your code during review** — the prompt is read-and-report; it needs the write bit for temp files and test runners.

```bash
# Write the prompt to a file: avoids ARG_MAX and shell-quoting issues. If you
# assembled $REVIEW_PROMPT with cat/heredoc, make sure the PR/issue bodies were
# appended RAW (never via an unquoted heredoc) or backticks in the PR body get
# command-substituted.
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
# It has also hung silently on every observed run. The in-prompt "diff HEAD vs
# origin/$BASE_BRANCH" instruction makes `exec` a faithful substitute.
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

echo "$REVIEW_TEXT"
```

(Note: `--base` is deliberately omitted. Codex infers the diff from the in-prompt instruction to diff `HEAD` vs `origin/$BASE_BRANCH`.)

This may take 3-10 minutes (longer if the retry fires). That is normal — Codex is doing a thorough, file-aware review, and with `workspace-write` it may also be running the test suite.

## Phase 2: Parse the Review

Read the final `VERDICT:` line — it is authoritative. Then cross-check against the findings:

1. **`VERDICT: LGTM`** (and no `[P1]`/`[P2]` tags of any kind remain) → APPROVED, skip to Phase 4.
2. **`VERDICT: CHANGES_REQUESTED`**, or any `[P1]`/`[P2]` tag (`[AC]`, `[SECURITY]`, or correctness) present → proceed to Phase 3.
3. **`VERDICT: BLOCKED`** → **count the findings before acting; the verdict line alone is not the signal.** There are three distinct causes:
   - **Real block** — a `[P1]/[P2]` `[AC]`/`[SECURITY]` finding needs a fix too big for this PR. Do NOT approve. Open a follow-up issue with the remediation plan, report the PR as **incomplete/blocked**, and stop the loop.
   - **Degraded** — the gateway failed but Codex inspected the diff anyway and returned real findings (look for "findings come from manual committed-diff inspection"). **The findings are valid** — treat them on their merits and continue the loop.
   - **Infra abort** — zero findings, and the prose blames Codex's own tooling ("Gateway self-check failed"; "Blocker is review infrastructure, not requested code changes"). This is a **failed** review, not an approval and not a code block. Re-run with a writable `TMPDIR` and `-s workspace-write`. If it still aborts, say the review is **inconclusive** — never approve on it, and never present your own test runs as if they were the independent gate.
4. **`NO_REVIEW_OUTPUT`** → DO NOT treat as approved. Empty output means Codex failed to run AND the `exec` fallback also produced nothing. Inspect `$CODEX_ERR` for the cause (auth, rate-limit, ARG_MAX, prompt-too-large, network). The runner already retried once via the fallback; do not loop indefinitely. If still empty, abort the loop and report status `inconclusive` with the stderr head — never auto-merge on inconclusive review.
5. **`PARTIAL_REVIEW …`** (prefix) → Codex did real analysis but hung before the final consolidated VERDICT/AC-matrix message (common on the `exec` fallback). The streamed `agent_message`s are included — read them. If they contain substantive across-the-gates findings with **no `[P1]`/`[P2]` tags** (Codex's text explicitly confirming the fix/clean gates), treat it like a clean run for gating purposes but, per the verdict-line-unreliability rule, **require two such consecutive P1/P2-free runs** before approving. If any `[P1]`/`[P2]` is present in the streamed text, proceed to Phase 3. Do NOT approve on a single partial with no corroboration.

Print the full review text (including the AC Coverage Matrix) so the user can see what Codex found.

## Phase 3: Fix Issues

For each `[P1]` and `[P2]` issue, first classify it:

- **`[AC]` or `[SECURITY]` finding** → the carve-out applies. Fix it even if it touches a non-diff file, **provided** the fix is minimal and direct. If the fix is broad/architectural/risky → escalate to **BLOCKED** (follow-up issue), do not jam it in. **Never** dismiss an `[AC]`/`[SECURITY]` finding as scope expansion or same-axis escalation.
- **Same-axis escalation** (correctness concern from a prior iteration, now a stricter variant of an already-agreed fix): **dismiss** per the Anti-Escalation Rule. Record in history. Do not fix.
- **Scope expansion** of a *correctness/smell* finding (fix requires editing untouched files or restructuring unrelated code): **dismiss** as out-of-scope. Record in history with a one-line follow-up note. Do not fix.
- **Genuine new correctness bug on a realistic path**: fix it.

For each issue you decide to fix:

1. Read the file mentioned in the review
2. Understand the issue Codex described
3. Fix it properly (address the root cause, don't just suppress)
4. Make any in-scope companion changes (tests, config). If the fix sprawls beyond the carve-out, reclassify as BLOCKED.

After all fixes:

```bash
git add -A
git commit -m "fix: address Codex nuclear review feedback (iteration N)

Fixed issues:
- [P1] description...
- [P2][AC] description...
- [P2][SECURITY] description...

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push
```

### Update Iteration History

After each iteration (fixed or dismissed), append a summary to `ITERATION_HISTORY`. This is passed to Codex next round.

For each issue Codex raised, record ONE outcome:

```
Iteration N:
- [P1] <issue> → FIXED: <what you changed>
- [P2][AC] <criterion> → FIXED: <what you changed, file:line>
- [P2][SECURITY] <vuln> → FIXED: <what you changed>
- [P1] <issue> → DISMISSED (escalation): same axis as iter K's <finding>; stricter variant of already-fixed concern
- [P2] <issue> → DISMISSED (out-of-scope): would require editing <file> the PR does not touch; follow-up issue
- [P1][AC] <criterion> → BLOCKED: needs <broad change>; follow-up issue #M opened; PR incomplete
- [P3] <issue> → NOTED (minor, not blocking)
```

Example:
```
Iteration 3:
- [P1] SQL injection in user_logs query → FIXED: parameterized the query in services.py:45
- [P2][AC] "export must include archived rows" missing → FIXED: added archived filter toggle in export.py:88
- [P1][SECURITY] IDOR — report endpoint lacks ownership check → FIXED: added org-scope guard in views.py:120
- [P2] Missing index on user_id column → FIXED: added index in migration 0007
- [P3] Consider adding type hints to helpers → NOTED (minor, not blocking)
```

Then **go back to Phase 1** with the next iteration number.

## Phase 4: Approved — Summary

When all three gates pass (or max iterations reached / BLOCKED), print:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ PR #$ARGUMENTS — Nuclear Review Complete
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Iterations: N
Result: APPROVED / MAX_ITERATIONS_REACHED / BLOCKED

Gate status:
- Correctness: PASS / N open
- AC alignment: PASS (all explicit criteria covered) / N missing-or-partial
- Security:    PASS / N open

AC Coverage Matrix (final):
[paste the matrix from the last review]

Review History:
- Iteration 1: [summary]
- Iteration 2: [summary]
...

Fixes Applied:
- [list of commits pushed]

Follow-up issues opened (if any):
- #M: [blocked item + remediation plan]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Important Notes

- **Sandbox:** run `codex exec - -s workspace-write --ephemeral --json` with `TMPDIR` pointed at a writable dir. Read-only starves the `gh-workflow-suite` gateway self-test (`tempfile.TemporaryDirectory` → `No usable temporary directory found`), which makes it fail closed to `BLOCKED`, and it also prevents Codex from running a single test. `workspace-write` is for temp files and test runners, not code edits — the worktree stayed clean across verified runs.
- **`codex exec review` is retired:** it rejects `-s`/`-a`/`--full-auto` (so it can never leave read-only) and hangs silently after `turn.started`. Use `codex exec` only. Parse **all** `agent_message` events, not just the last — `exec` may hang before the final summary, and its intermediate messages carry the real findings (`PARTIAL_REVIEW`). (macOS lacks `timeout`/`setsid` — the watchdog backgrounds codex and kills on stall instead. STALL_SECS=420 / MAX_SECS=1800; 150s killed healthy runs mid-review.)
- Tags: `[P1]` critical, `[P2]` major, `[P3]` minor — suffixed with `[AC]` (acceptance criteria) or `[SECURITY]` for those gates. Only `[P1]`/`[P2]` block approval.
- The iteration history is passed to Codex each round so it knows what was fixed/dismissed. If Codex re-raises a *correctness* issue already dismissed, skip it. **Never** skip a re-raised `[AC]`/`[SECURITY]` issue on those grounds.
- Approval is gated on the `VERDICT:` line AND zero open `[P1]`/`[P2]` across all three gates — not on fuzzy phrases like "looks good".
- Preserve the PR's original intent. The only sanctioned scope expansion is the minimal direct fix for an explicit AC gap or a real security hole.
