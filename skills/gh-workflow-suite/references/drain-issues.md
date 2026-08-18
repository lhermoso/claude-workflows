# Drain Issues

## Contents

- Inputs and terminal states
- Inventory and dependency DAG
- Wave execution
- Sequential review and merge
- Continuation and final report

## Inputs and terminal states

Defaults are two parallel issue writers, all quality gates enabled, and serial
auto-merge after gates pass. Cap normal writer concurrency at three. Honor label,
`--max-parallel=N`, `--dry-run`, `--no-merge`, `--skip-review`, `--basic-review`,
`--no-plan-review`, `--no-verify`, and `--get-all`.

Create one run-shared reviewer-provider state before spawning workers. Claude is
preferred until it fails to produce a valid, conclusive review; then every
remaining gate in every worker uses the sticky fresh read-only Codex fallback.
Pass the same absolute provider-state path to every worker; workers must not
create their own. A new `drain-issues` run tries Claude again.

Make every logical gate ID unique across the whole drain run. Prefix it with its
issue or PR number plus stage, round, and a short SHA when applicable (for
example, `issue-42-full-review-r1-a1b2c3d4e5f6`), so workers sharing the provider
state never collide. Never rotate an ID to retry a consumed Codex fallback.

Freeze the eligible issue set at run start unless the user explicitly asks for a
live backlog. Track each issue as one of:

```text
PENDING | CLAIMED | IMPLEMENTING | REVIEWING | READY | MERGED
SKIPPED | BLOCKED_DEP | HELD | FAILED
```

Stop when the frozen run set has no actionable issue. Do not loop forever because
blocked, failed, or newly created issues remain open.

## Inventory and dependency DAG

1. Resolve the authenticated GitHub user and repository default branch.
2. Fetch all eligible open issues with bodies, labels, assignees, and enough
   comments to understand dependency statements; do not silently cap at 50.
3. Unless `--get-all`, skip issues assigned only to other users.
4. Distinguish real blockers (`depends on`, `blocked by`, prerequisite work) from
   umbrella/tracking references that merely list children.
5. Detect missing dependencies and cycles. Mark cycle members and descendants
   `BLOCKED_DEP`; keep independent work actionable.
6. Build topological waves. Show issue, dependency, assignment, and wave tables.
7. With `--dry-run`, stop here before assignment or any Git/GitHub mutation.

Assignment is coordination, not an atomic lock. Immediately before work, re-read
assignees/state, claim the whole wave, and skip an issue that another actor took.

## Wave execution

For each wave:

1. Fetch the base again. A dependent issue must start from a base containing its
   merged prerequisites.
2. Create one unique branch, worktree, state record, and PR per issue.
3. Spawn at most `--max-parallel` Codex writers while reserving capacity for the
   root scheduler. A worker owns one issue and must not recursively fan out.
4. Have each worker follow `issue-pipeline.md` through PR creation and readiness,
   with no merge. Pass through the drain options.
5. Return compact structured results: issue, worktree, PR, head SHA, gate SHAs,
   status, blockers, and test/check summaries.
6. Preserve failed worktrees. Continue independent issues even when one fails.

The root owns adherence rechecks, final review sequencing, and merge decisions.
Never allow two writers in one worktree.

## Sequential review and merge

Review and merge one PR at a time. Never batch final merge decisions.

For each PR:

1. If `--skip-review`, leave it open and mark `HELD`; do not auto-merge.
2. Otherwise require a valid review-gateway result for the exact current head.
   Use `full-review.md`, or one shorter structured gateway pass with
   `--basic-review`. Record whether Claude or Codex fallback produced it.
3. Require plan adherence unless `--no-verify`, local tests, lint/typecheck,
   mergeable state, and successful required GitHub checks for the same SHA.
   Billing, spending-limit, quota, provider, and runner failures are not waivers.
   A job that did not start or executed no meaningful steps is missing CI
   evidence. Local parity may diagnose it but cannot satisfy its required GitHub
   status; restore CI and rerun the same SHA.
4. Fetch the latest base immediately before merge. If rebasing or merging the
   base changes the head, invalidate and rerun local, adherence, reviewer, and
   CI gates. A base-tip change that changes the effective diff also requires a
   fresh review snapshot.
5. Re-read `headRefOid` immediately before merge and require:

```text
current head == local gate SHA == adherence SHA == review SHA == CI SHA
```

6. Never use `--admin` or bypass protection unless the user explicitly supplied
   `--admin-merge`. Infrastructure failure never implies that option. If
   approval policy prevents self-merge, comment with the gate record and mark
   `HELD`.
7. With `--no-merge`, mark a passing PR `READY` and leave it open. Do not start a
   dependent issue unless the user explicitly requested stacked branches.
8. Otherwise use the repository's configured merge method; if none is known,
   use rebase consistently. After success, mark `MERGED` and clean its worktree.

After one PR merges, refresh the base for every remaining PR in the wave.
Prioritize revalidation where changed-file sets overlap.

## Continuation and final report

After a wave barrier:

- Unblock descendants only when every prerequisite is actually present on their
  effective base.
- Propagate a failed/held prerequisite to descendants as `BLOCKED_DEP` while
  continuing independent branches.
- Refresh the DAG and start the next actionable wave.
- Do not assume a tracking issue auto-closes when its listed children close.

Finish with counts and tables for merged, ready, held, failed, blocked,
skipped, and unprocessed issues. Include PR URLs, exact gate SHAs, retained
worktrees, recovery commands, and why every non-merged issue stopped. Include
one compact provider/trigger summary for the run. Show per-gate providers only
when they differed materially or the user requested an audit.
