# Additional Migrated Workflows

Use these compact workflows for suite commands other than `full-review`,
`issue-pipeline`, and `drain-issues`. Apply all shared runtime rules from
`porting-notes.md`.

Every review step below means `scripts/run_review.py` with one provider-state
file for the workflow run. Claude is preferred; any failed, invalid, or
inconclusive Claude review activates the fresh read-only Codex fallback. The
fallback remains fail-closed.

Assign each logical gate a stable run-unique ID containing the command, target,
stage, and round. A new snapshot or review round gets a new ID. Never rotate an
ID to retry Codex.

## Commit

1. Refuse to commit on the default branch.
2. Inspect staged, unstaged, and untracked changes; preserve unrelated changes.
3. Stage only the paths authorized by the request.
4. Run relevant verification and inspect the staged diff.
5. Create a conventional commit when the user asked for a commit. Do not add AI
   attribution.

## Pull Request

1. Confirm a non-default branch and clean intended scope.
2. Fetch the default branch and resolve divergence deliberately.
3. Test, stage explicit paths, and commit the intended implementation before any
   merge-gating review. A review of uncommitted changes is advisory only.
4. Push and create a draft PR with a structured body and changelog update when
   appropriate.
5. Run a read-only review of the committed PR head through `run_review.py` and
   an immutable snapshot. Prefer Claude; use the fresh Codex fallback whenever
   Claude cannot produce a valid, conclusive review.
6. Fix blocking findings in new commits, retest, push, and re-review after every
   mutation. Mark the PR ready only after the current committed head passes.

## Fix Issue and Quick Fix

1. Fetch the full issue body and comments.
2. Create a unique worktree and branch from the fetched default branch.
3. Diagnose the root cause and write a test first when supported.
4. Implement the smallest complete fix, test, lint/typecheck, update the
   changelog when user-visible, and commit explicit paths.
5. Push and open a linked draft PR, then run the provider-gateway review against
   its committed head. Put each accepted fix in a new commit, retest, push, and
   re-review before marking ready.
6. For Quick Fix, minimize commentary but keep the same safety and review gates.

## Create Issue

1. Infer bug, feature, enhancement, task, or documentation from the request.
2. Draft a precise title, context, reproduction when relevant, and measurable
   acceptance criteria.
3. Verify labels before using them.
4. Create the issue immediately when the user requested creation; otherwise
   present the draft for confirmation.

## Review PR and Review Changes

1. Build an immutable diff snapshot and relevant issue/PR context.
2. Invoke `run_review.py`. Claude is preferred; if it fails, returns invalid
   output, or returns `INCONCLUSIVE`, accept one fresh ephemeral read-only Codex
   reviewer against the identical snapshot. Never let the active authoring task
   directly produce the gate result.
3. Return findings first with severity and file references.
4. Do not edit unless the user separately asked to address findings.
5. Interact with GitHub only when requested. Comment instead of trying to approve
   a self-authored PR.

For `review-changes` on staged or unstaged files, hash and preserve the exact
working-tree patch, then create a separate clean detached worktree at the source
`HEAD`. Run the advisory gateway review from that clean worktree with the frozen
patch as context; never point the gateway at the user's dirty checkout. Treat
the result as advisory: uncommitted content cannot satisfy a PR/merge gate.
Commit first and run a new SHA-bound review for gating.

## Scan Debt

1. Scope the scan to the requested repository or path.
2. Inspect TODO/FIXME/HACK markers, type escapes, debug artifacts, tracked
   secrets, oversized files, missing tests, and ecosystem-specific audits.
3. Use the review gateway when prioritization affects a merge or release gate;
   keep successful fallback silent until the final compact provenance summary.
4. Report highest-risk findings first. Create GitHub issues only when requested.

## Batch Issues

1. Resolve filters such as label, range, assignee, or all-open.
2. Create disjoint worktrees and issue ownership.
3. Use bounded Codex workers, one writer per issue, following Fix Issue.
4. Aggregate PRs and failures. Do not merge unless the user requested a merge
   workflow.

For dependency-aware repeated processing, use `drain-issues.md` instead.
