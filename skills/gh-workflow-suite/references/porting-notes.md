# Runtime and Porting Rules

## Contents

- Role mapping
- State and snapshot rules
- Review provider gateway
- User-visible communication
- Git and GitHub rules
- Subagent and worktree rules
- Compatibility options

## Role mapping

| Original concept | Codex-native behavior |
| --- | --- |
| Claude slash command | `$gh-workflow-suite` plus a workflow name |
| Claude host agent | Root Codex task or bounded Codex worker |
| Claude `Task` | Codex subagent with one worktree and one issue |
| Claude `Workflow` fan-out | Bounded root-owned workers or one review gate |
| Preferred reviewer | Fresh read-only `claude -p` process |
| Claude review fallback | Fresh ephemeral `codex exec` process in a read-only sandbox |
| Reviewer-requested edits | Active Codex task verifies and applies accepted findings |
| Plan mode | `update_plan` plus durable run state |

The active Codex task owns planning, implementation, tests, and fixes. It may
start another Codex process only through the bundled review gateway after Claude
fails to produce a valid, conclusive review. The fallback process is a reviewer,
never a writer, and cannot resume the authoring session.

## State and snapshot rules

Store workflow state outside tracked files. Prefer:

```bash
GIT_COMMON_DIR=$(git rev-parse --path-format=absolute --git-common-dir)
RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
RUN_ROOT="$GIT_COMMON_DIR/gh-workflow-suite/$RUN_ID"
RUN_STATE="$RUN_ROOT/state"
mkdir -p "$RUN_STATE"
chmod 700 "$RUN_ROOT" "$RUN_STATE"
```

When state is stored inside Git metadata, the gateway permits writes only under
`$GIT_COMMON_DIR/gh-workflow-suite/`. It rejects `.git/HEAD`, refs, the index,
config, and every other Git control path. Paths wholly outside the worktree and
Git metadata are also valid.

Record issue, branch, worktree, PR, gate results, attempts, reviewer provider,
and blockers in a manifest. Record the exact SHA for every gate. Keep one shared
`reviewer-provider.json` beneath the run root so Codex fallback remains sticky
for that run, including all `drain-issues` workers. A new workflow run starts by
trying Claude again. Use these states where relevant:

```text
PLANNED -> PLAN_APPROVED -> IMPLEMENTED -> LOCAL_GREEN -> PR_OPEN
        -> ADHERENCE_GREEN -> REVIEW_GREEN -> CI_GREEN -> READY
```

Any commit, rebase, base merge, force-push, or external head change invalidates
`LOCAL_GREEN` and every later SHA-bound state. Restart at local verification.

For a review snapshot, record:

- `head_sha`: exact PR head.
- `base_sha`: fetched base branch tip.
- `merge_base_sha`: merge base used for the patch.
- PR metadata and linked issue context.
- Changed-file list and patch.
- Iteration history and plan/adherence report, when present.

Before trusting a review, re-read local `HEAD` and GitHub `headRefOid`. Reject a
result if either differs from the snapshot.

## Review provider gateway

For each gate, create fresh, non-overlapping directories. Put only reviewer
evidence in `CONTEXT_DIR`, the prompt in `INPUT_DIR`, and all outputs in
`ARTIFACT_DIR`; never reuse an artifact or state directory as reviewer context.

```bash
# Run-global and target-namespaced; stable only for this exact logical gate.
GATE_ID="pr-123-full-review-01"
INPUT_DIR="$RUN_ROOT/inputs/$GATE_ID"
CONTEXT_DIR="$RUN_ROOT/evidence/$GATE_ID"
ARTIFACT_DIR="$RUN_ROOT/artifacts/$GATE_ID"
mkdir -p "$INPUT_DIR" "$CONTEXT_DIR" "$ARTIFACT_DIR"
chmod 700 "$INPUT_DIR" "$CONTEXT_DIR" "$ARTIFACT_DIR"
```

Create a short provider-neutral prompt that points the reviewer only to files
in `CONTEXT_DIR`. Do not put a large diff on argv or interpolate GitHub text
into shell source. Run every plan, adherence, basic, and full review gate
through:

```bash
python3 <skill-root>/scripts/run_review.py \
  --prompt "$INPUT_DIR/review-prompt.md" \
  --schema <skill-root>/references/review-schema.json \
  --output "$ARTIFACT_DIR/review.json" \
  --trace-output "$ARTIFACT_DIR/review-trace.json" \
  --error-file "$ARTIFACT_DIR/reviewer.stderr" \
  --provider-state "$RUN_STATE/reviewer-provider.json" \
  --metadata-output "$ARTIFACT_DIR/review-provider.json" \
  --cwd "$WORKTREE" \
  --context-dir "$CONTEXT_DIR" \
  --gate-id "$GATE_ID" \
  --expected-head "$HEAD_SHA" \
  --expected-base "$BASE_SHA" \
  --expected-merge-base "$MERGE_BASE_SHA" \
  --effort "$REVIEW_EFFORT" \
  --timeout "$REVIEW_TIMEOUT"
```

Choose budget by gate instead of giving every review the maximum window:

| Gate | Effort | Timeout |
| --- | --- | ---: |
| Diagnosis or fix-impact plan review | `medium` | 300s |
| Plan adherence or basic diff review | `high` | 600s |
| Final full PR review | `high` | 900s |

For diagnosis and fix-impact, build a bounded evidence pack before invoking the
gateway. Include issue criteria, current plan/claims, exact relevant paths,
focused source excerpts, caller/search results, state/lifecycle notes, and test
evidence. Do not hand the reviewer an open-ended instruction to discover the
whole repository. Permit extra `Read`/`Grep`/`Glob` only on the listed relevant
paths when the pack leaves a concrete claim unresolved.

The gateway tries Claude first with an argv array, no shell, safe mode, no
session persistence, `dontAsk`, and only `Read,Grep,Glob`. If Claude is
unavailable, times out, exceeds quota, returns malformed output, or returns a
valid `INCONCLUSIVE` verdict, it starts exactly one Codex fallback session
against the same prompt, context, schema, and SHAs. That session is fresh, ephemeral,
approval-free, stripped of user config/rules/MCP/web search, and sandboxed
read-only. It receives the prompt only on stdin. Valid `APPROVED`,
`CHANGES_REQUESTED`, and `BLOCKED` verdicts are final and never trigger fallback.

Every frozen prompt receives a gateway-generated semantic output contract in
addition to the JSON schema. This contract exposes invariants enforced by the
validator, including the complete security-category set. If the first Codex
generation completes but fails schema or semantic validation, the same fallback
session may launch exactly one fresh repair generation against the identical
snapshot. Give it the validator error and the same frozen contract/evidence. Do
not repair timeouts, nonzero exits, snapshot movement, or substantive verdicts.

A plan reviewer must prioritize a timely structured verdict over exhaustive
exploration. Its prompt must tell it to stop searching after the supplied claims
and relevant paths are checked, return `INCONCLUSIVE` when evidence is genuinely
insufficient, and never spend remaining time searching unrelated code.

The locked run-shared provider state atomically consumes a gate's Codex fallback
session before spawning it. IDs must be unique across the entire workflow run
and all workers, not merely within one PR. Reusing that `GATE_ID` can never start
another fallback session after a malformed result, exit `6`, crash, or
interruption. One live session may contain the initial generation plus its one
validator-guided repair. Choose a new ID only for a genuinely new
plan/adherence/review stage, review round, or snapshot; do not rotate IDs to
retry a failed session. In `drain-issues`, prefix IDs with the issue or PR number
so concurrent workers cannot collide.

Fallback applies to missing/auth failures, generic `429` or temporary rate
limits, overload, timeout, network/transport errors, invalid models, context
limits, local budget caps, malformed/schema-invalid output, quota exhaustion,
and `INCONCLUSIVE`. Do not fall back for valid `CHANGES_REQUESTED` or `BLOCKED`
verdicts. If the Codex fallback still fails after its allowed repair, return
`INCONCLUSIVE`; never start another fallback session.

The gateway validates provider output, schema invariants, mandatory SHA binding,
AC blockers, security coverage, and verdict consistency. Exit codes are
`0=APPROVED`, `10=CHANGES_REQUESTED`, `11=BLOCKED`, and `12=INCONCLUSIVE`;
Claude infrastructure/schema failures trigger fallback; a failed Codex fallback
uses `6`. A Git/input snapshot mismatch uses `7`. Only exit zero
is approval. Always read `ARTIFACT_DIR/review.json` and the runner-generated
`ARTIFACT_DIR/review-provider.json`; never trust model-reported provenance.
Keep `review-trace.json` and diagnostics private and untracked.

Before starting a provider, the gateway requires a clean Git worktree, resolves
the actual `HEAD`, verifies the expected base and merge-base commit objects, and
recomputes the merge base. It rejects `assume-unchanged`, `skip-worktree`, sparse,
or other nonstandard index visibility that could hide working-tree changes. It
freezes the prompt, schema, and context directories into private read-only copies
used by both providers, while keeping attempt logs in provider-specific,
sequential inaccessible scratch directories. Claude's
scratch is destroyed before a fallback Codex process starts. The gateway rejects
any prompt, state, or artifact path nested in reviewer context and rechecks Git
state after every attempt. A mismatch is infrastructure failure, never approval
or fallback.

Optional environment variables:

- `CLAUDE_REVIEW_MODEL`: model alias or full model name. Omit to use the user's
  configured Claude default.
- `CLAUDE_REVIEW_EFFORT`: defaults to `medium`. Final full review passes
  `--effort high` explicitly.
- `CLAUDE_REVIEW_MAX_BUDGET_USD`: optional positive per-call budget cap.
- `CLAUDE_BIN`: alternate Claude executable.
- `CODEX_REVIEW_MODEL`: optional model for the fallback reviewer.
- `CODEX_BIN`: alternate Codex executable.

Switch directly to Codex after any Claude failure or `INCONCLUSIVE` verdict.
Never start a second Codex fallback session or override a substantive
`CHANGES_REQUESTED`/`BLOCKED` verdict automatically. Only the one
validator-guided repair generation is allowed inside the consumed session.

## User-visible communication

Treat successful provider fallback as internal routing, not a progress event.
Do not announce Claude timeout, provider switching, trace contents, reviewer
agreement, or statements such as "not pretending Claude approved anything."
Continue with the workflow and discuss findings or implementation progress only.

Use `review-provider.json` for provenance. A first fallback has
`fallback_used=true` and `provider_selected_from_state=false`; later gates have
`provider_selected_from_state=true`. Neither condition should produce ordinary
commentary. Mention fallback only:

- once in the final report as a compact provider/trigger field;
- when the user explicitly asks about reviewer provenance; or
- immediately when fallback fails or returns `INCONCLUSIVE` and blocks work.

The root must create exactly one provider-state file per workflow run and pass
its absolute path to every gate and worker. Workers must never create or replace
their own provider state. Once one gate switches the shared state to Codex, no
later gate or worker in that run may invoke Claude.

## Git and GitHub rules

- Prefer the GitHub app for metadata when available and `gh` for shell-centric
  operations such as worktrees, pushes, checks, and merges.
- Read the complete issue thread, including comments, before planning.
- Resolve the remote default branch explicitly. If it cannot be resolved, stop;
  do not silently assume `main`.
- Fetch the base before creating a worktree and after every prerequisite merge.
- Never auto-stash, reset, or absorb a dirty worktree. Preserve user changes.
- Use `git diff --no-ext-diff --no-textconv --find-renames` for snapshots so
  repository-defined diff drivers cannot execute.
- Run targeted tests, the broadest practical suite, lint/typecheck, and
  `git diff --check` after every mutation.
- Stage an allowlist and inspect `git diff --cached --name-only` before commit.
- Do not commit state directories, `.pair/`, secrets, or unrelated user files.
- Use conventional commits without `Co-Authored-By` or generated-by text.
- Never treat an empty check list as proof that CI passed. Distinguish no checks
  configured from checks still missing or inaccessible.
- Billing is not a waiver. Treat a GitHub Actions billing, spending-limit, quota,
  provider, or runner failure as the cause of missing CI evidence, never as a
  pass or permission to skip, downgrade, satisfy, approve, mark ready, publish,
  merge, or bypass a required check.
- Treat jobs that did not start or executed no meaningful steps as missing.
  Local parity may diagnose the change but cannot replace a required GitHub
  status context. Restore CI and rerun the exact same SHA.
- Require explicit user authorization for any separate branch-protection policy
  change. Never infer or request an implicit waiver from infrastructure failure.

## Subagent and worktree rules

- Give each issue one unique `codex/issue-<n>-<run-id>` branch and worktree.
- Allow exactly one writer per worktree. Reviewers remain read-only.
- Keep issue workers at depth one; the root scheduler owns any verification
  fan-out and merge sequence.
- Bound concurrency by the user's option, available agent slots, and machine
  resources. Default to two writers and cap at three unless the user overrides.
- Never run `gh pr checkout` in the user's primary checkout. Use `git -C
  "$WORKTREE"` or create a dedicated PR worktree.
- Preserve failed or blocked worktrees with recovery instructions. Remove a
  successful worktree only after durable PR comments/state are written.

## Compatibility options

For `issue-pipeline` and `drain-issues`, plan review, adherence verification,
and full provider-gateway review are on by default.

- `--no-plan-review`: skip the pre-implementation reviewer plan gate.
- `--no-verify`: skip plan-adherence verification.
- `--basic-review`: use one shorter provider-gateway diff review, still
  structured, fallback-aware, and fail-closed.
- `--skip-review`: leave PRs open without a review gate; never auto-merge them.
- `--no-merge`: create and review PRs but do not merge.
- `--no-improve`: skip targeted pre-final-review improvement passes.
- `--dry-run`: perform no assignment, worktree, branch, issue, PR, or merge
  mutation.
- `--get-all`: include issues assigned to other users.
- Legacy `--plan-review` and `--full-review` are accepted no-ops because those
  gates are already enabled.
