# claude-workflows

A collection of slash commands for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that automate the full developer workflow — from issue creation to PR merge.

Drop these into `~/.claude/commands/` and get a complete CI-like pipeline inside your terminal.

## Quick overview

| Command | Description |
|---------|-------------|
| [`/commit`](#commit) | Stage changes and create conventional commits |
| [`/pr`](#pr) | Create detailed PRs with documentation and changelog |
| [`/fix-issue`](#fix-issue) | End-to-end issue fix with worktrees and TDD |
| [`/quick-fix`](#quick-fix) | Autonomous fix — minimal checkpoints, max speed |
| [`/create-issue`](#create-issue) | Create well-structured GitHub issues |
| [`/review-pr`](#review-pr) | Review a PR for changelog alignment and merge safety |
| [`/review-changes`](#review-changes) | Meticulous diff review for breaking changes |
| [`/scan-debt`](#scan-debt) | Scan for tech debt, code smells, and security issues |
| [`/batch-issues`](#batch-issues) | Process multiple issues in parallel |
| [`/drain-issues`](#drain-issues) | Dependency-aware wave processing until backlog is empty |
| [`/issue-pipeline`](#issue-pipeline) | Full pipeline: create issue → fix → PR → review |
| [`/full-review`](#full-review) | Claude ↔ Codex iterative review loop |

---

## Commands in detail

### `/commit`

Stage changes and create a well-crafted [conventional commit](https://www.conventionalcommits.org/).

```bash
/commit
```

**What it does:**
1. Checks you're not on `main`/`master`
2. Reviews staged and unstaged changes
3. Proposes a conventional commit message (`fix`, `feat`, `refactor`, etc.)
4. Waits for your confirmation before committing

---

### `/pr`

Create a detailed pull request with proper documentation.

```bash
/pr
```

**What it does:**
1. Verifies branch safety and rebases on the default branch
2. Runs `/review-changes` to check for issues
3. Crafts a structured PR description (summary, problem, solution, testing, impact)
4. Creates the PR via `gh` and updates `CHANGELOG.md` if present

---

### `/fix-issue`

Complete workflow to fix a GitHub issue — from checkout to PR.

```bash
/fix-issue <issue-number>
```

```bash
/fix-issue 42
```

**What it does:**
1. Creates a git worktree from the default branch (isolated workspace)
2. Reads and analyzes the issue
3. Enters plan mode — proposes root cause and solution, waits for approval
4. Writes a failing test first, then implements the fix (TDD)
5. Runs full test suite
6. Self-reviews with `/review-changes`
7. Commits, pushes, and creates PR linked to the issue
8. Updates changelog if present
9. Provides cleanup instructions for the worktree after merge

---

### `/quick-fix`

Fast autonomous fix with minimal human checkpoints.

```bash
/quick-fix <issue-number>
```

```bash
/quick-fix 42
```

**How it differs from `/fix-issue`:**
- Enters plan mode for approval but otherwise runs autonomously
- Auto-retries on test failures (max 2 attempts)
- Auto-creates PR and self-reviews
- Only stops for ambiguity or critical errors

---

### `/create-issue`

Create a well-structured GitHub issue with clear acceptance criteria.

```bash
/create-issue <brief description>
```

```bash
/create-issue position sync fails when wallet has no active positions
/create-issue add volatility regime detection to hedge engine
```

**What it does:**
1. Determines issue type (bug, feature, enhancement, task)
2. Drafts title, description, and acceptance criteria
3. Verifies labels exist before using them
4. Waits for confirmation before creating

---

### `/review-pr`

Review a PR for changelog alignment and merge safety.

```bash
/review-pr <pr-number>
```

```bash
/review-pr 123
```

**What it checks:**
- Changelog entry exists and matches the PR type
- No debug code, secrets, or commented-out blocks
- Function signatures and API contracts unchanged (or documented as breaking)
- Tests added for new code paths
- PR size (flags >500 lines)
- CI/CD status
- Detects if it's your own PR (uses `--comment` instead of `--approve`)

---

### `/review-changes`

Meticulous review of recent code changes to catch breaking changes and regressions.

```bash
/review-changes
```

**What it checks:**
- Function signature and return type changes
- Removed or renamed exports
- Surface-level fix detection (flags z-index hacks, retry loops, bare null checks)
- LCP candidate modifications (frontend)
- CHANGELOG.md path and formatting
- All usages of modified functions still compatible

**Output:** Structured report with severity levels (HIGH/MEDIUM/LOW) and fix recommendations.

---

### `/scan-debt`

Scan codebase for technical debt, code smells, and improvement opportunities.

```bash
/scan-debt              # scan entire repo
/scan-debt src/         # scan specific directory
```

**What it scans for:**

| Category | Examples |
|----------|---------|
| Debt markers | `TODO`, `FIXME`, `HACK`, `@deprecated` |
| Type safety | `as any`, `@ts-ignore`, `.unwrap()`, `unsafe` |
| Debug artifacts | `console.log`, `dbg!`, `print(` |
| Security | tracked `.env` files, hardcoded secrets, AWS keys (`AKIA`), GitHub tokens |
| Architecture | files >500 lines, deeply nested directories, magic numbers |
| Test gaps | source files without corresponding test files, disabled tests |
| Dependencies | outdated packages, known vulnerabilities |

**Output:** Prioritized report (Critical → Low) with actionable recommendations. Optionally creates GitHub issues for significant findings.

---

### `/batch-issues`

Process multiple issues in parallel using subagents. Simple parallel execution without dependency analysis.

```bash
/batch-issues 12,15,18                    # specific issues
/batch-issues 10-15                       # range
/batch-issues label:bug                   # all issues with label
/batch-issues assignee:@me               # your assigned issues
/batch-issues all-open                    # all open issues (careful!)
/batch-issues "authentication error"      # search by keyword
```

**What it does:**
1. Gathers the issue list based on your filter
2. Shows the list and waits for confirmation
3. Launches parallel subagents (max 3) — each in its own worktree
4. Each agent: fetches issue body + all comments → writes failing test → fixes → creates PR
5. Aggregates results into a summary table
6. Optionally batch-reviews all created PRs

**Use `/drain-issues` instead if your issues have dependencies on each other.**

---

### `/drain-issues`

Autonomous issue processor — analyzes dependencies between issues, batches independent ones into waves, and repeats until the backlog is empty.

```bash
/drain-issues                                        # process all open issues
/drain-issues label:bug                             # only bugs
/drain-issues label:enhancement --max-parallel=3    # enhancements, 3 at a time
/drain-issues --dry-run                             # analyze only, don't process
/drain-issues --no-merge                            # create PRs but don't auto-merge
/drain-issues --skip-review                         # create PRs without review (faster)
/drain-issues --full-review                         # Claude↔Codex review loop per PR
/drain-issues label:bug --full-review --no-merge    # combine flags
/drain-issues --get-all                             # include issues assigned to others
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `label:<name>` | *(none)* | Only process issues with this label |
| `--max-parallel=N` | `2` | Max concurrent subagents (max 3) |
| `--dry-run` | `false` | Analyze dependencies and plan waves without executing |
| `--no-merge` | `false` | Create and review PRs but don't auto-merge |
| `--skip-review` | `false` | Create PRs without the review phase |
| `--full-review` | `false` | Use Claude↔Codex review loop instead of basic review |
| `--get-all` | `false` | Process all open issues regardless of assignment. By default, issues already assigned to someone else are skipped |

**How it works:**
1. Fetches all open issues (or filtered subset), skipping issues assigned to others (unless `--get-all`)
2. Analyzes dependencies between issues (explicit `#ref`, shared files, sequential chains)
3. Groups independent issues into waves
4. Presents wave plan and waits for confirmation
5. **Assigns itself** to all issues in the wave before starting work (claims them)
6. Processes each wave: parallel subagents in worktrees → PRs → review → merge
7. Cleans up worktrees after each merge
8. Moves to next wave until all issues are processed
9. Final summary with merged PRs, failed items, and cleanup commands

**Wave example:**
```
Wave 1: [#12, #15, #22] — independent issues, run in parallel
Wave 2: [#18, #28]       — depend on wave 1 results
Wave 3: [#24]            — depends on #18 from wave 2
```

**Safety features:**
- Max 3 parallel agents to prevent context overflow
- Confirmation before each wave
- Auto-pause on >50% failure rate
- Worktree isolation (no conflicts between parallel fixes)
- No force git operations

---

### `/issue-pipeline`

Full pipeline: create an issue (if needed), fix it, create a PR, and self-review — all in one command.

```bash
# Fix an existing issue
/issue-pipeline 42

# Describe a new issue — it creates it, then fixes it
/issue-pipeline add rate limiting to the API
/issue-pipeline fix the login timeout when wallet has no positions

# With Codex review loop
/issue-pipeline 42 --full-review
/issue-pipeline add dark mode toggle --full-review

# With plan refinement loop before implementation
/issue-pipeline 42 --plan-review

# Full gauntlet: plan review + Codex review loop
/issue-pipeline 42 --plan-review --full-review
```

**Options:**

| Flag | Description |
|------|-------------|
| `--plan-review` | Claude↔Codex plan refinement loop before implementation (up to 3 rounds) |
| `--full-review` | Claude↔Codex iterative review loop after implementation |

**How it works:**
1. **Detects mode:** number → fix existing issue; text → create new issue first
2. **Creates issue** (if text input): auto-detects type from keywords (bug/feature/enhancement)
3. **Sharpens the problem statement:** Codex pre-analyzes the issue and produces a precise problem statement (root cause hypothesis, affected files, edge cases, success criteria)
4. **Spawns a subagent** that: creates worktree → investigates root cause → writes failing test → creates implementation plan
5. **Plan review loop** (`--plan-review`): Codex reviews the plan and flags issues → Claude refines → repeat up to 3 rounds → implement with the best plan
6. **Implements** the fix with minimal changes → runs tests + linter → updates changelog → commits → creates PR
7. **Reviews the PR:** basic review (default) or Claude↔Codex loop (`--full-review`)
8. **Improvement passes:** up to 2 Codex-powered quality passes on the finished implementation (better abstractions, naming, edge cases)
9. **Merges if safe**, or reports what needs attention

---

### `/full-review`

Claude ↔ Codex review loop. Codex reviews the PR, Claude fixes issues, repeat until Codex approves or max iterations.

```bash
/full-review <pr-number>
```

```bash
/full-review 123
```

**Requirements:** [Codex CLI](https://github.com/openai/codex) (`codex`) must be installed.

**How it works:**
1. Checks out the PR branch
2. Runs Codex in `--full-auto` mode to review the diff
3. Parses Codex output for severity tags:
   - `[P1]` Critical — must fix
   - `[P2]` Major — must fix
   - `[P3]` Minor — noted but not blocking
4. Fixes all `[P1]` and `[P2]` issues, commits, and pushes
5. Maintains an **iteration history** passed to Codex each round so it won't re-raise fixed/dismissed issues
6. Repeats until Codex approves or 15 iterations reached
7. Prints a summary with all iterations and fixes applied

---

## Installation

### As user-level commands (available in all projects)

```bash
git clone https://github.com/lhermoso/claude-workflows.git
cp claude-workflows/*.md ~/.claude/commands/
```

### As project-level commands (available only in a specific project)

```bash
# From your project root
mkdir -p .claude/commands
cp path/to/claude-workflows/*.md .claude/commands/
```

### Verify installation

Open Claude Code and type `/` — you should see the commands in autocomplete.

## Key design principles

- **Git worktrees for isolation** — `/fix-issue`, `/quick-fix`, `/batch-issues`, and `/drain-issues` create worktrees so you can work on multiple issues in parallel without conflicts
- **Full issue context** — Commands fetch the issue body *and all comments* before touching code. Reproduction steps, design decisions, and constraints often live in the thread, not the original description
- **Plan before building** — `/issue-pipeline --plan-review` runs a Claude↔Codex refinement loop on the implementation plan before writing a single line of code. Up to 3 rounds: original plan → Codex critique → Claude refines
- **Root cause over surface fixes** — Commands explicitly warn against z-index hacks, retry loops, and other band-aids. They push for understanding *why* before fixing
- **Test-driven fixes** — Write a failing test first, then implement the fix
- **Conventional commits** — All commit messages follow the [Conventional Commits](https://www.conventionalcommits.org/) spec
- **Changelog awareness** — Commands auto-detect and update `CHANGELOG.md` when present
- **No AI attribution** — Commits and PRs are clean — no "Co-Authored-By" or "Generated by Claude" lines
- **Branch safety** — Every command checks you're not on `main`/`master` before making changes

## Requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI
- [GitHub CLI](https://cli.github.com/) (`gh`) — for issue/PR commands
- Git
- [Codex CLI](https://github.com/openai/codex) *(optional)* — only needed for `/full-review`

## License

MIT
