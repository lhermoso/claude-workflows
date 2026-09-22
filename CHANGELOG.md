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
  replaces the flattened `/import` artifact with the full skill package.
  Reviewer routing is described under Changed (Astra / Fable / Astra): a
  configurable primary per gate class with the other provider as a sticky,
  fail-closed fallback
- **`/issue-pipeline` + `/drain-issues`**: **Plan-Adherence Verification** (Verification Phase / Phase 5.5) — after implementation, a Workflow fans out one verifier agent per plan claim (acceptance criteria, file changes, test plan, side-effect invariants) against the actual worktree code + PR diff; DIVERGED/MISSING findings are adversarially confirmed by two independent refuters; a reverse-trace agent maps diff hunks back to claims to surface **unplanned changes**. Output is an **Implementation Report** posted as a PR comment (✅ as planned · 🔀 diverged · ❌ missing · ➕ unplanned). Confirmed-missing AC/test claims trigger a fix-and-reverify cycle; in `/drain-issues`, PLAN_NOT_MET PRs are blocked from auto-merge. Falls back to issue acceptance criteria when no `.pair/PLAN.md` exists. Opt out with `--no-verify`

### Changed

- **Code review now receives the plan, not just the diff — the full REQUIREMENTS → PLAN → DIFF → REVIEW → FIXES → CI cycle** (`/full-review`, `/issue-pipeline`, `/drain-issues`):
  - **Three inputs to Codex, every iteration:** original requirements (linked issues with comments + PR body), the implementation plan (`.pair/PLAN.md` plus the Implementation Report, now also written to `.pair/REPORT.md` by the Verification Phase / Phase 5.5), and the diff. `/full-review` takes `--plan=` / `--report=` and auto-detects `.pair/` or the `## Implementation Report` PR comment; the pipeline and the drainer always pass both. Previously `.pair/PLAN.md` never reached the reviewer (gitignored, worktree-local) and the pipeline's review step said literally `Iteration 1: review`
  - **Four gates instead of three:** correctness, requirements (AC matrix), **architecture** (diff vs the plan's Proposed Fix / Side-Effects Trace, starting from the report's 🔀/➕ items; N/A without a plan), security — plus an explicit **defect checklist** every gate answers with evidence: race conditions, state inconsistencies, database issues, performance regressions, missing edge cases, missing tests
  - **Severity is `BLOCKER` / `HIGH` / `MEDIUM` / `LOW`** with axis tags `[CORRECTNESS]` / `[AC]` / `[ARCH]` / `[SECURITY]` / `[CI]`; only BLOCKER and HIGH block. Replaces `[P1]`/`[P2]`/`[P3]` (legacy mapping: P1→BLOCKER, P2→HIGH, P3→MEDIUM)
  - **Mandatory finding format:** file, relevant code (quoted), why it is a problem, reproduction scenario, proposed correction. Codex proposes and never edits; the fable reviewer triages (ACCEPT / DISMISS / RECORDED for harmless `[ARCH]` deviations), the opus coder applies. A finding without a reproduction is capped at HIGH (not for `[AC]`/`[SECURITY]`); the runner reverts any worktree edit Codex leaves behind
  - **CI gate (new Phase 3.5):** Codex LGTM alone is no longer approval. `/full-review` polls `gh pr checks` on the final SHA and requires every required status context green (`CI_GREEN`); `CI_FAILED` becomes a `[BLOCKER][CI]` finding and re-enters the fix + review loop; `CI_MISSING` (a check that never ran — billing, quota, runner) is missing evidence, never a pass. `/drain-issues` never merges without `CI_GREEN`, holds `CI_MISSING` PRs with a comment, stops the drain when CI is down for the whole wave, and never uses `--admin` to bypass a check. `/issue-pipeline` re-runs the gate after improvement passes and reports `CI:` in its final output. Local test runs, Codex's or the coder's, never substitute
  - The Verification Phase (fable, plan-adherence) is kept as an independent gate: it produces the report Codex starts from, and it is the adherence check that still runs when Codex is unavailable
- **Codex skill `gh-workflow-suite` mirrors the cycle as Astra / Fable / Astra** — Codex plans and orchestrates, a fresh Claude Fable process implements, a fresh read-only Codex process reviews:
  - **New implementer runner `scripts/run_claude_implement.py`** + `references/implement-schema.json`: runs `claude -p --safe-mode --permission-mode dontAsk` on Fable inside one worktree from `plan.md` + `context.md`, with a fixed contract (no Git mutations, failing test first, no test weakening, `plan_rejected` instead of improvising). After the run it verifies `HEAD` and the index did not move and that the declared `changed_files` equal the worktree's actual changes; structured result carries `tests_written`, `commands_run`, `plan_deviations`, `brief_gaps`. Exit codes `0/10/11` = success / plan_rejected / failed. The active Codex task never writes production code anymore; it plans, triages findings into fix lists, verifies, stages, commits, pushes
  - **Review gateway `scripts/run_review.py` generalized to `--primary {codex,claude}`** (default `codex`, env `REVIEW_PRIMARY_PROVIDER`) with the other provider as the sticky fallback, tracked per primary in provider state v3 (`routing` / `fallbacks` / `fallback_attempted_gates`). Code review (adherence, basic, full) is Codex-primary because Fable wrote the code; plan gates are Claude-primary because Codex wrote the plan — the reviewer vendor always differs from the writer. `--writer` (default `claude`) feeds the `independent_vendor_review` provenance flag. The one validator-guided repair generation now applies to whichever provider is the fallback
  - **Review schema v2** (`references/review-schema.json`, validators in `run_claude_review.py`): severity `BLOCKER/HIGH/MEDIUM/LOW` (only the first two block), category adds `ARCH` (valid only when `plan_provided`), findings require `relevant_code`, `failure_mode`, `reproduction`, `minimal_fix`; new top-level `plan_provided`, `architecture_summary`, and a six-item `defect_checklist` (races, state, DB, perf, edge cases, tests) that must be fully checked for any conclusive verdict; explicit MISSING criteria must be BLOCKER, explicit PARTIAL at least HIGH. Legacy `schema_version: 1` reviews are rejected
  - **Reviewer inputs and gates** (`references/full-review.md`): `CONTEXT_DIR` gains `plan.md`, `implementation-report.md`, and `test-evidence.md`; the prompt runs four gates (correctness, requirements, architecture, security) plus the defect checklist, forbids rewriting code, and asks for the mandatory finding format. New **CI gate** section between `APPROVED` and publication: `CI_FAILED` goes back through the implementer and a new review round, `CI_MISSING` is never a pass; `drain-issues` holds such PRs and stops the drain when a whole wave is held. The reviewer stays read-only on this side (it does not run tests; test evidence reaches it as files)
  - `SKILL.md`, `agents/openai.yaml`, `porting-notes.md` (role mapping, implementer runner section, env vars), `issue-pipeline.md`, `drain-issues.md`, `workflows.md` rewritten for the new roles; installer manifest includes the new files. Self-tests: gateway 31, review validator 13, implementer 12
- **`codex exec review` is banned everywhere — it discards findings, not just the VERDICT
  line**: `/drain-issues` (review loop) and `/issue-pipeline` (full-review loop) still invoked
  the subcommand; both now run `printf '%s' "$PROMPT" | codex exec - -s read-only --ephemeral
  --json`, with the `HEAD` vs `origin/$BASE_BRANCH` diff instruction in-prompt. Measured on a
  real drain (COTIntelligence, 8 branches): on one branch the subcommand returned a clean
  249-character review *after 7 genuine file reads*, while the same prompt through plain
  `codex exec` found two real defects and returned `VERDICT: CHANGES_REQUESTED`; across all
  eight branches it never once emitted the VERDICT line. Each of `/drain-issues`,
  `/issue-pipeline` and `/full-review` now records the consequence explicitly: **a zero-finding
  review from `codex exec review` is not evidence that a branch is clean — it is no evidence at
  all**, and must be discarded and re-run through plain `codex exec`. `/full-review` had already
  retired the subcommand for hanging silently; that note now carries the dropped-findings
  evidence too, since a silent hang is at least visible and a false clean review is not.
  Supersedes the `codex exec review` half of the 0.136.0 flag fix below (#14, #16); the stdin
  `-` form, the `--model` ban, the foreground-in-subagent rule and the `-a`/`--full-auto`
  gotchas are unchanged
- **`/drain-issues` + `/issue-pipeline` planner agents must run Codex in the foreground**: planners were
  launching the plan review with `run_in_background` and ending the turn to wait for a
  task notification that can never arrive — a subagent is not woken by its own background
  task, so the agent went idle until the orchestrator noticed (~20 min lost per planner,
  observed on two waves). The Model Routing rules now state that every `Task` subagent
  (planner, reviewer, coder) invokes `codex exec` as a blocking call and stays in the same
  turn until it returns, polling `BashOutput` in-turn if anything is backgrounded; only the
  main orchestrator loop may background work and rely on being re-invoked. The same rule is
  repeated inside each planner prompt's plan-review bookkeeping so it lands in the subagent's
  own context
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
  <br>**Superseded:** the `codex exec review` guidance in this entry is no longer valid — the subcommand is banned outright (see Changed, above). Its flag notes are kept only as a record of what was true on 0.136.0.

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
