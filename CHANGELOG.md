# Changelog

All notable changes to claude-workflows are documented here.

Format: `[version] - YYYY-MM-DD`
Types: `Added`, `Changed`, `Fixed`, `Removed`

---

## [Unreleased]

### Added

- **Codex `gh-workflow-suite`**: added complete `full-review`, `issue-pipeline`,
  and `drain-issues` skill references; a bounded no-shell reviewer gateway;
  strict JSON review schema; and a conservative user-global installer that
  replaces the flattened `/import` artifact with the full skill package. Claude
  is preferred, with one fresh read-only Codex fallback whenever Claude cannot
  produce a valid, conclusive review
- **`/issue-pipeline` + `/drain-issues`**: **Plan-Adherence Verification** (Verification Phase / Phase 5.5) — after implementation, a Workflow fans out one verifier agent per plan claim (acceptance criteria, file changes, test plan, side-effect invariants) against the actual worktree code + PR diff; DIVERGED/MISSING findings are adversarially confirmed by two independent refuters; a reverse-trace agent maps diff hunks back to claims to surface **unplanned changes**. Output is an **Implementation Report** posted as a PR comment (✅ as planned · 🔀 diverged · ❌ missing · ➕ unplanned). Confirmed-missing AC/test claims trigger a fix-and-reverify cycle; in `/drain-issues`, PLAN_NOT_MET PRs are blocked from auto-merge. Falls back to issue acceptance criteria when no `.pair/PLAN.md` exists. Opt out with `--no-verify`

### Changed

- **`/drain-issues` + `/issue-pipeline` plan review is now a single Codex pass**: the
  two-pass, multi-round loop (Pass A diagnosis, max 2 rounds → Pass B fix-impact, max 3
  rounds, up to 5 Codex calls per issue) collapses into **one** Codex review covering
  diagnosis *and* fix side-effects in a single prompt. Claude writes `.pair/PLAN.md`,
  Codex reviews it once against the real code in the worktree, Claude absorbs the
  findings into the plan (`[WRONG]` → Diagnosis, `[BUG]` → Side-Effects Trace + Files,
  `Missing From Plan` → added, rejected items → `## Dismissed` + `unresolved[]`), then
  coding starts. There is no re-review round, so the `DIAGNOSIS_CONFIRMED` /
  `FIX_APPROVED` verdicts are gone — they only existed to gate the loop. The planner
  JSON's `plan_review` field is now `reviewed|skipped|unavailable` (was
  `confirmed|max_rounds|skipped|unavailable`), and a `plan_rejected` escalation has the
  planner revise the plan directly instead of re-running Codex. Empty-Codex handling is
  unchanged: retry once, then hand off the unreviewed plan as `unavailable`
- **`/drain-issues` + `/issue-pipeline` context handoff**: the planner now writes a
  `.pair/CONTEXT.md` brief — files read with the regions that matter, real signatures
  with line numbers, entry points, repo conventions, exact test/lint commands, dead
  ends already ruled out, and what it did *not* read — and the coder starts from that
  instead of re-exploring. Coders are explicitly barred from broad grep/glob sweeps and
  orientation reads; they open a file only to edit it, to cover a gap the brief flags,
  or to correct the brief, and report shortfalls back via `brief_gaps` so the template
  can be fixed once instead of paying the re-read every issue. The Phase 5.5 / Verification
  reviewer and the full-review fix loop consult the same brief, and Codex `[P1]`/`[P2]`
  fix lists must now be self-contained (file, region, and current code) so the coder never
  re-reads to locate a site. `.pair/CONTEXT.md` is gitignored worktree-local scratch like
  the rest of `.pair/`
- **`/drain-issues` + `/issue-pipeline` model routing**: roles are now split across
  models instead of running everything on the session model. Planning (root-cause
  investigation, `.pair/PLAN.md`, running the Codex plan review) and reviewing
  (plan-adherence verification + Implementation Report, Codex `[P1]`/`[P2]` triage,
  basic diff review, improvement-pass triage, merge decision) run on
  **claude-fable-5**, with a single automatic retry on **opus** if fable is
  unavailable — never a downgrade to a smaller model. All code — failing test,
  implementation, lint/test runs, changelog, commits, PRs, and applied review
  fixes — is written by **claude-opus-5**. Consequences: the monolithic fix
  subagent is split into a planner agent and a coder agent (the coder returns
  `plan_rejected` with evidence instead of silently redesigning; one planner
  round-trip is allowed); reviewers emit fix lists but never edit files;
  plan-adherence verification moves from the main loop into one sequential
  reviewer agent per PR (still one `gh pr diff`, still no fan-out). Overridable
  per run with `--plan-model=`, `--code-model=`, `--review-model=`. Codex is
  untouched and still runs on its own default model
- **Codex port**: inverted the pair workflow so Codex is the only writer and the
  local Claude CLI is the preferred independent read-only reviewer. Confirmed
  any failed, invalid, or inconclusive Claude review now selects a sticky fresh
  ephemeral Codex reviewer for the rest of the workflow run; the fallback
  remains fail-closed. Review results are bound to immutable head/base/merge-base SHAs,
  with evidence, prompt, provider state, artifacts, and provider scratch kept
  separate so fallback reviewers cannot ingest prior raw attempts, and all
  mutations invalidate local, adherence, review, and CI gates. A durable gate
  ledger prevents a resumed or crashed gate from invoking Codex more than once
- **Codex review UX**: successful Claude-to-Codex fallback is now silent during
  execution. Provider provenance remains in artifacts and appears once in the
  final report; only failed or inconclusive fallback interrupts commentary
- **Codex plan-review latency**: diagnosis and fix-impact gates now require
  curated evidence, medium effort, and a five-minute timeout. The 15-minute
  high-effort budget is reserved for final full PR review
- **Codex review contract repair**: the gateway now appends its hidden semantic
  invariants to every frozen provider prompt, including all mandatory security
  categories. A completed invalid Codex fallback receives one validator-guided
  repair generation inside the already-consumed fallback session
- **`/issue-pipeline` + `/drain-issues`**: **all quality gates are now ON by default** — plan review (a single Codex pass), plan-adherence verification, and the full Claude↔Codex review loop run without flags. New opt-out flags: `--no-plan-review`, `--no-verify`, `--basic-review` (fast diff review instead of the Codex loop). Legacy `--plan-review`/`--full-review` flags are accepted but redundant. Fix subagents now return their worktree path and must not remove worktrees before verification reads `.pair/PLAN.md`

- **`/drain-issues`**: `--plan-review` flag — optional Codex plan review loop before implementation; each subagent writes a plan, Codex critiques it (max 3 rounds), then implements the refined plan; catches design issues early before any code is written
- **Codex port**: Added [`skills/gh-workflow-suite`](skills/gh-workflow-suite), a Codex-native skill that ports the repo's Claude workflows to Codex skills and review primitives

### Changed

- **`/full-review`**: upgraded into a **nuclear review** with three gates — correctness, acceptance-criteria alignment, and security; approval now requires all three. Phase 0 reads the PR description and resolves linked issues (`closingIssuesReferences` + a `Closes/Fixes/Resolves #N` body-regex fallback) to pull their acceptance criteria into the review. Codex emits an **AC Coverage Matrix** (missing/partial explicit AC blocks via a hard `VERDICT:` contract) and an explicit **security gate** with a vuln-class checklist and exploitability-based severity. An **AC/security carve-out** lets blocking `[AC]`/`[SECURITY]` findings override the anti-scope-expansion rule (broad fixes go `BLOCKED` + follow-up issue). Fixes the Phase 1 Codex command (single stdin-piped run; version-safe `-s workspace-write -a never` — `--full-auto` errors on the `review` subcommand) — Fixes #11
- **`/issue-pipeline` + `/drain-issues`**: `--plan-review` is now a two-pass Claude↔Codex review against the **actual code** in the worktree. Pass A verifies diagnosis (max 2 rounds); Pass B traces the proposed fix's side-effects (max 3 rounds). Plans use a structured 7-section format (`.pair/PLAN.md`) including a **Side-Effects Trace** and a **What I Am Most Likely Wrong About** paragraph. Codex runs with `--sandbox read-only` and stdin-piped prompts, returns structured output (Confirmed / Bugs Introduced / Missing / Verdict), and review history accumulates in `.pair/REVIEW.md` across rounds so dismissed issues are not re-raised. Both `.pair/` files are committed alongside the fix. Unresolved `[BUG]` items at round limit surface in the PR body under `## Unresolved Codex concerns` — Fixes #9

### Fixed

- **`/full-review` + `/issue-pipeline` + `/drain-issues`**: corrected Codex invocations to flags that actually parse on codex-cli 0.136.0 — prompts piped via stdin (positional args silently hang); plain `codex exec` uses `-s <mode>` only (non-interactive auto-approves — `-a`/`--ask-for-approval` is a global flag and errors after `exec`); `codex exec review` uses only `--ephemeral --json --title` (it rejects `-s`, `-a`, and `--full-auto`, and runs read-only by default); the `/issue-pipeline` full-review loop uses `codex exec review -` without `--base` (mutually exclusive with a custom prompt), diffing in-prompt — Fixes #14, #16

## [1.2.0] - 2026-03-13

### Fixed

- **`/drain-issues`**: Umbrella/epic issues no longer block their sub-issues from being claimed and processed — tracking references (`- [ ] #12` checklists) are no longer treated as blocking dependencies

### Added

- **`/drain-issues`**: Explicit umbrella/epic detection in Phase 2 — issues identified by title keywords (`Epic`, `Umbrella`, `Tracking`, `Meta`), checklist body pattern, or self-description as trackers
- **`/drain-issues`**: Umbrella placement rule in Phase 3 — epics with their own implementation work go into Wave 1 alongside sub-issues; purely tracking epics are skipped

### Changed

- **`/drain-issues`**: Dependency analysis now distinguishes *blocking references* (`depends on`, `blocked by`, `after #X`, `requires #X`) from *tracking references* (umbrella → sub-issues); only blocking references create wave dependencies
- **`/issue-pipeline`**: `--plan-review` flag — optional Claude↔Codex plan refinement loop before implementation; Claude writes a plan, Codex critiques it, Claude refines, repeat up to 3 rounds (1 original + 2 refinements); proceeds to implement with the best plan regardless
- **`/issue-pipeline`**: Prompt Enhancement Phase — Codex pre-analyzes the issue before the fix agent runs, producing a precise problem statement with root cause hypothesis, affected files, edge cases, and success criteria
- **`/issue-pipeline`**: Improvement Passes (post-implementation) — up to 2 Codex-powered quality passes after review completes; each pass generates a fresh prompt in a new context window focused on improving abstraction, naming, and edge case coverage
- **`/fix-issue`**: Tool inventory block in plan mode — explicitly lists all available tools and requires the plan to include steps for running tests, linter, and type checker
- **`/fix-issue`**: Guardrails updated — linter/type checker is now a required pre-commit step alongside the full test suite
- **`/issue-pipeline`**: Step 6 now explicitly instructs agents to account for all available tools when creating implementation plans

## [1.1.0] - 2026-03-13

### Added

- **`/drain-issues`**: Self-assign issues at wave start — claims all issues in the wave before launching subagents, preventing conflicts in team environments
- **`/drain-issues`**: Assignment filter — skips issues already assigned to someone else by default
- **`/drain-issues`**: `--get-all` flag — override the assignment filter and process all open issues regardless of who they are assigned to
- **All issue commands**: Full comment context — `gh issue view` now fetches `comments` field so agents read the full issue thread (body + all comments) before touching code. Affected: `/drain-issues`, `/quick-fix`, `/batch-issues`, `/issue-pipeline`
- **README**: New design principle — *Full issue context*
- **CHANGELOG.md**: This file

### Changed

- **`/drain-issues` Phase 1**: Fetch payload now includes `assignees` and `comments` fields
- **`/drain-issues` Phase 4**: New Step 4.0 runs `gh issue edit --add-assignee @me` for every issue before subagents start
- **`/batch-issues`**: Subagent prompt now explicitly fetches issue with comments before analysis
- **`/issue-pipeline`**: Fix phase subagent now fetches comments as a dedicated step
- **`/quick-fix`**: Context fetch updated to include `comments` in `--json` fields
- **README**: `/drain-issues` how-it-works steps and options table updated

---

## [1.0.0] - 2026-02-01

### Added

- `/commit` — Stage changes and create conventional commits
- `/pr` — Create detailed PRs with documentation and changelog
- `/fix-issue` — End-to-end issue fix with worktrees and TDD
- `/quick-fix` — Autonomous fix with minimal checkpoints
- `/create-issue` — Create well-structured GitHub issues
- `/review-pr` — Review a PR for changelog alignment and merge safety
- `/review-changes` — Meticulous diff review for breaking changes and regressions
- `/scan-debt` — Scan for tech debt, code smells, and security issues
- `/batch-issues` — Process multiple issues in parallel using subagents
- `/drain-issues` — Dependency-aware wave processing until backlog is empty
- `/issue-pipeline` — Full pipeline: create issue → fix → PR → review
- `/full-review` — Claude ↔ Codex iterative review loop
