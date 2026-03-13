# Changelog

All notable changes to claude-workflows are documented here.

Format: `[version] - YYYY-MM-DD`
Types: `Added`, `Changed`, `Fixed`, `Removed`

---

## [Unreleased]

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
