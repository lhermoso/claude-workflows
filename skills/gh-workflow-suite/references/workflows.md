# Workflows

## Shared Guardrails

- Stay off the default branch for commit and PR creation flows.
- Read the full GitHub issue thread, not just the top-level body, before implementing fixes.
- Prefer root-cause fixes over surface-level patches.
- Write or update a failing test first for bug-fix workflows when the project has tests.
- Run targeted verification plus the broadest practical test and lint/typecheck commands before committing.
- Stage specific files instead of broad `git add .` or `git add -A` when avoidable.
- Update the repo-root `CHANGELOG.md` when the repository uses one and the change is user-facing.

## Commit

Use for the former `/commit` flow.

1. Verify the current branch is not the default branch.
2. Inspect staged, unstaged, and untracked changes.
3. Stage the intended files if the user asked for a commit but nothing is staged yet.
4. Propose a conventional commit message based on the actual diff.
5. Ask before running `git commit` unless the user clearly asked for an immediate commit.

## Pull Request

Use for the former `/pr` flow.

1. Confirm the branch is not the default branch and the working tree is ready.
2. Fetch and rebase or merge the default branch as appropriate for the repo.
3. Run the `Review Changes` workflow or `codex review --base origin/<default-branch>` before opening the PR.
4. Draft a structured PR body covering summary, problem, solution, testing, impact, and breaking changes.
5. Update `CHANGELOG.md` if appropriate.
6. Push the branch and create the PR with the GitHub plugin or `gh pr create`.

## Fix Issue

Use for the former `/fix-issue` flow.

1. Fetch the issue body, labels, and all comments.
2. Prefer creating a worktree from the default branch. If sandbox rules block that, create a dedicated branch in the current workspace instead.
3. Investigate the root cause before writing code.
4. Publish a short plan with `update_plan`. Pause only if the user requested a checkpoint or the change is risky or ambiguous.
5. Write a failing test first when the project supports it.
6. Implement the smallest fix that addresses the root cause.
7. Run the new test, broader tests, and lint or typecheck commands.
8. Self-review with `Review Changes` or `codex review`.
9. Update changelog, commit, push, and create a PR linked to the issue.

## Quick Fix

Use for the former `/quick-fix` flow.

1. Follow the `Fix Issue` workflow with fewer checkpoints.
2. Proceed autonomously after a brief plan unless ambiguity is material.
3. Retry verification once or twice if failures look mechanical rather than conceptual.
4. Stop and surface the blocker if the issue remains unclear or verification keeps failing.

## Create Issue

Use for the former `/create-issue` flow.

1. Infer the issue type from the request: bug, feature, enhancement, task, or documentation.
2. Draft a tight title, description, context, reproduction steps if relevant, and acceptance criteria.
3. Verify labels before using them.
4. Ask for confirmation before creating the issue unless the user explicitly asked you to create it now.
5. Create the issue with the GitHub plugin or `gh issue create`.

## Review PR

Use for the former `/review-pr` flow.

1. Fetch PR metadata, commits, changed files, diff, and CI status.
2. Determine whether the PR belongs to the authenticated user. If so, comment instead of approving or requesting changes.
3. Review for changelog alignment, debug code, secrets, breaking changes, test coverage, PR size, and merge safety.
4. Return findings first with severity and file references.
5. If the user asked you to interact on GitHub, submit a review comment, approval, or change request using the GitHub plugin or `gh`.

## Review Changes

Use for the former `/review-changes` flow.

1. Determine the diff range against the merge base with the default branch.
2. Inspect changed files for breaking contracts, removed exports, changed signatures, async or sync changes, schema changes, and risky surface-level fixes.
3. Search call sites and related code paths with `rg`.
4. Return a findings-first review with severity, file references, and concrete fixes.

## Scan Debt

Use for the former `/scan-debt` flow.

1. Determine the target scope: whole repo or a specific path.
2. Scan for TODO/FIXME/HACK markers, type escapes, debug artifacts, tracked secrets, oversized files, and missing tests.
3. Run dependency audit commands that make sense for the detected ecosystem, such as `npm audit` or `cargo audit`, when available.
4. Summarize by priority with the highest-risk findings first.
5. Offer issue creation only if the user wants the findings turned into GitHub issues.

## Batch Issues

Use for the former `/batch-issues` flow.

Interpret literal carry-over options like `label:bug`, `10-15`, `assignee:@me`, and `all-open` as filters.

If the user explicitly requested parallel or delegated execution:

1. Gather the issue list and confirm the intended subset if the scope is broad.
2. Spawn at most three workers with disjoint issue ownership and isolated branches or worktrees.
3. Have each worker follow the `Fix Issue` or `Quick Fix` workflow for its assigned issue.
4. Aggregate PR numbers, failures, and cleanup notes in the main thread.

If the user did not explicitly request delegation:

1. Explain that the Codex port runs this workflow sequentially by default.
2. Process the selected issues one by one in the main thread.

## Drain Issues

Use for the former `/drain-issues` flow.

Interpret former flags such as `--dry-run`, `--max-parallel=N`, `--skip-review`, `--no-merge`, `--full-review`, `--plan-review`, and `--get-all` as user options when they appear literally.

1. Fetch open issues with assignment data and any label filter.
2. Skip issues assigned to someone else unless the user explicitly asked for all issues.
3. Analyze blocking dependencies, distinguishing true blockers from umbrella or tracking references.
4. Build waves of independent issues.
5. If `--dry-run` is present, stop after showing the wave plan.
6. If the user explicitly requested delegation, process each wave with up to the allowed number of workers. Otherwise process each wave sequentially in the main thread.
7. Review, merge, and clean up according to `--skip-review`, `--no-merge`, and `--full-review`.

## Issue Pipeline

Use for the former `/issue-pipeline` flow.

Interpret a bare number as an existing issue and free text as a request to create a new issue first.

1. If the request is text, run the `Create Issue` workflow first.
2. Sharpen the problem statement locally before implementation. Only shell out to a fresh `codex exec` context if a clean second pass is materially useful.
3. Follow the `Fix Issue` workflow for the resulting issue.
4. If `--plan-review` is present, do an explicit plan-review pass before coding. This can be a local refinement pass or a fresh `codex exec` call if a separate context helps.
5. After implementation, run the `Review PR` or `Full Review` workflow depending on whether `--full-review` is present.

## Full Review

Use for the former `/full-review` flow.

1. Fetch the PR metadata, check out the PR branch, and identify the base branch.
2. Run a fresh Codex review pass, preferably with a command like `codex exec review --base origin/<base-branch> --title "<pr-title>" --ephemeral -o /tmp/codex-review.txt`. (Do NOT pass `--full-auto`/`-s`/`-a` to `codex exec review` — the review subcommand rejects them; it runs read-only and non-interactive by default. `-o`/`--output-last-message` is supported by `codex exec review`, but not by the bare `codex review` alias.)
3. Treat clearly blocking or high-severity findings as must-fix. If the output includes `[P1]` or `[P2]`, use those tags directly.
4. Fix only files that are already part of the PR scope unless an adjacent test or config change is necessary.
5. Commit and push each iteration, keeping a short iteration history so repeated review passes do not revisit closed issues.
6. Stop when the review is effectively clean or when the maximum iteration limit is reached.
