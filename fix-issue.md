---
allowed-tools: Bash(git:*), Bash(gh:*), Bash(grep:*), Bash(find:*), Bash(cat:*), Bash(npm:*), Bash(cargo:*), Bash(pnpm:*)
argument-hint: <issue-number>
description: Complete workflow to fix a GitHub issue - from checkout to PR
---

# Fix Issue Workflow

## Context

Current branch:
!`git branch --show-current`

Repository status:
!`git status --short`

Issue details:
!`gh issue view $ARGUMENTS --comments 2>/dev/null || echo "Could not fetch issue $ARGUMENTS - will need manual lookup"`

Related recent commits:
!`git log --oneline -10`

---

## Phase 1: Setup with Git Worktrees (MANDATORY)

**IMPORTANT: You MUST use git worktrees for all issue fixes. Do NOT work directly in the main repository.**

Git worktrees allow you to work on multiple branches simultaneously in separate directories, each with its own Claude instance. This isolation is required to prevent conflicts and ensure clean implementations.

1. **Detect default branch and fetch latest**
   ```bash
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")
   git fetch origin $DEFAULT_BRANCH
   ```

2. **Create a worktree for this fix**
   ```bash
   # Check if worktree directory already exists
   if [ -d "../fix-$ARGUMENTS-<description>" ]; then
     echo "Worktree directory already exists. Reusing it."
     cd ../fix-$ARGUMENTS-<description>
   else
     git worktree add ../fix-$ARGUMENTS-<description> -b fix/$ARGUMENTS-<description> origin/$DEFAULT_BRANCH
     cd ../fix-$ARGUMENTS-<description>
   fi
   ```

   This creates:
   - A new directory `../fix-$ARGUMENTS-<description>` alongside your main repo
   - A new branch `fix/$ARGUMENTS-<description>` based on `origin/$DEFAULT_BRANCH`

3. **List active worktrees** (from any worktree)
   ```bash
   git worktree list
   ```

**Why worktrees are required:**
- Work on multiple issues in parallel with separate Claude instances
- No need to stash/commit before switching context
- Each worktree has its own node_modules, build cache, etc.
- Clean separation between different tasks
- Prevents accidental changes to the main working directory

---

## Phase 2: Understand the Issue

Thoroughly analyze issue #$ARGUMENTS:

1. **Read the issue description** - What is the reported problem?
2. **Review comments** - Any additional context or reproduction steps?
3. **Check labels/assignees** - Priority? Related components?
4. **Find related issues** - Is this a duplicate or related to other work?
   ```bash
   gh issue list --search "related keywords" --limit 5
   ```

**Before proceeding, summarize:**
- What is broken?
- What is the expected behavior?
- What are the reproduction steps?

---

## Phase 3: Investigation

1. **Locate relevant code**
   - Search for keywords from the issue using the native **Grep** and **Glob** tools
   - Find the affected files/functions
   - Use **Read** tool to examine file contents

2. **Trace the code path**
   - Understand how data flows through the affected area
   - Identify entry points and dependencies

3. **Identify root cause (CRITICAL)**
   - **Do NOT apply surface-level fixes** (e.g., z-index, overflow-hidden, retry loops). Find the UNDERLYING problem first.
   - Ask "why does this happen?" repeatedly until you reach the actual cause
   - Check git blame if needed: `git blame <file>`
   - For UI/CSS bugs: identify the actual box model/layout issue (e.g., negative margins, incorrect positioning) before reaching for z-index or overflow hacks
   - For numerical/simulation bugs: verify all input parameters and config values are correct before assuming code logic is wrong

4. **Ask clarifying questions** if:
   - The issue is ambiguous
   - Multiple solutions are possible
   - There's missing context about business logic

---

## Phase 4: Plan & Propose

**IMPORTANT: Before creating the plan, you MUST switch to plan mode:**

```
Use the EnterPlanMode tool to switch to plan mode before drafting your implementation plan.
```

**Tool inventory for planning** — you cannot execute these now, but your plan MUST account for them. A plan that omits a "run tests" or "compile" step will produce an incomplete implementation. Available tools:
- `Bash` → run shell commands (tests, linting, type checking, build/compile)
- `Read`, `Grep`, `Glob` → explore and read codebase
- `Edit`, `Write` → modify and create files
- `gh` → GitHub CLI (create PRs, view issues, list labels)
- `git` → version control operations

Include explicit steps in your plan for: running the failing test, running the full test suite, running the linter/type checker. If you don't put them in the plan, you won't do them.

In plan mode, present a clear implementation plan:

```
### Root Cause
[Explain what's actually causing the issue]

### Proposed Solution
[High-level approach]

### Files to Modify
- `path/to/file1.ts` - [what changes]
- `path/to/file2.rs` - [what changes]

### Risks & Considerations
- [Potential side effects]
- [Edge cases to handle]

### Testing Strategy
- [How to verify the fix works]
```

**⏸️ CHECKPOINT: Use the ExitPlanMode tool to submit your plan for user approval before implementing**

---

## Phase 5: Test-Driven Implementation

**Write failing tests FIRST, then implement the fix.**

1. **Write a failing test that reproduces the bug**
   - Create a test that captures the exact broken behavior
   - Run it to confirm it FAILS (proving the bug exists)
   ```bash
   # Run just the new test to confirm it fails
   npm test -- --grep "bug description"   # JS/TS
   cargo test test_name                    # Rust
   pytest -k "test_name"                   # Python
   ```

2. **Implement the fix**
   - Follow existing code style and patterns
   - Keep changes minimal and focused
   - Add comments for non-obvious logic

3. **Verify the test now passes**
   ```bash
   # Same test should now pass
   npm test -- --grep "bug description"
   ```

4. **Enumerate and test edge cases**
   Think about related scenarios that might have the same root cause:
   - If it's an i18n bug → test ALL locales
   - If it's a numeric bug → test zero, negative, boundary values
   - If it's a null/undefined bug → test all nullable inputs
   - Write tests for at least 3 edge cases

5. **Run the FULL test suite**
   - Ensure no regressions in existing tests
   ```bash
   npm test        # Node.js
   cargo test      # Rust
   pytest          # Python
   ```

6. **Search for similar patterns**
   - Grep the codebase for similar code that might have the same bug
   - If found, fix those too with their own tests

---

## Phase 6: Verification

1. **Run tests**
   ```bash
   # Adjust for your project
   npm test        # Node.js
   cargo test      # Rust
   pytest          # Python
   ```

2. **Run linter/type checker**
   ```bash
   npm run lint && npm run typecheck   # TypeScript
   cargo clippy                        # Rust
   ```

3. **Manual verification**
   - Follow the original reproduction steps
   - Confirm the issue is resolved
   - Test related functionality hasn't broken

---

## Phase 7: Self-Review

**Run the `/review-changes` command to perform a meticulous code review:**

```
/review-changes
```

This will analyze your changes for:
- Breaking changes (function signatures, return types, exports)
- All usages updated
- Regressions in related functionality
- Error handling completeness
- Debug code removal
- Type safety
- Test coverage

---

## Phase 8: Commit

**Stage changes:**
```bash
# Review what will be staged first
git diff --name-only

# Stage specific files (preferred — avoids accidentally staging .env or credentials)
git add <specific-files>
```

**Create commit message following Conventional Commits:**

Format:
```
<type>(scope): <short description>

<detailed body explaining the what and why>

Fixes #$ARGUMENTS
```

Types: `fix`, `feat`, `refactor`, `docs`, `test`, `chore`

Example:
```
fix(positions): handle null balance in position sync

The position registry was crashing when a wallet had no active
positions due to an unchecked null reference. Added defensive
check and early return for empty position arrays.

Fixes #$ARGUMENTS
```

**IMPORTANT: DO NOT include any of the following in commits or PRs:**
- Co-Authored-By lines
- "Generated by Claude" or similar attribution
- Any reference to Claude, Anthropic, or AI assistance

**⏸️ CHECKPOINT: Show the commit message and wait for confirmation before committing**

---

## Phase 9: Push & PR

1. **Push the branch**
   ```bash
   git push -u origin HEAD
   ```

2. **Create Pull Request**
   ```bash
   gh pr create --title "fix: <description>" --body "Fixes #$ARGUMENTS

   ## Changes
   - <bullet points of what changed>

   ## Testing
   - <how it was tested>
   "
   ```

**Capture the PR URL for changelog entry.**

---

## Phase 10: Changelog Update

**If the project maintains a `CHANGELOG.md`, update it with this PR's changes.**

1. **Check if CHANGELOG.md exists**
   ```bash
   ls CHANGELOG.md 2>/dev/null
   ```

2. **If it exists, add entry under the appropriate `[Unreleased]` section:**

   ```markdown
   ### Fixed
   - Brief description of fix ([#PR](url))

   ### Added
   - Brief description of feature ([#PR](url))

   ### Changed
   - Brief description of change ([#PR](url))
   ```

3. **Commit and push:**
   ```bash
   git add CHANGELOG.md
   git commit -m "docs: update changelog"
   git push
   ```

> **Note:** If no `CHANGELOG.md` exists, skip this step. Do not create one unless the project explicitly requires it.

---

## Phase 11: Worktree Cleanup

After the PR is merged (or if you need to abandon the fix):

1. **Return to the main repository**
   ```bash
   cd ../your-main-repo-directory
   ```

2. **Remove the worktree**
   ```bash
   git worktree remove ../fix-$ARGUMENTS-<description>
   ```

   If there are uncommitted changes and you want to force removal:
   ```bash
   git worktree remove --force ../fix-$ARGUMENTS-<description>
   ```

3. **Prune stale worktree references** (optional, cleans up if directory was manually deleted)
   ```bash
   git worktree prune
   ```

4. **Delete the branch** (after PR is merged)
   ```bash
   git branch -d fix/$ARGUMENTS-<description>
   ```

---

## Summary Checklist

- [ ] **Worktree created from latest default branch (REQUIRED)**
- [ ] **NOT on master/main** (feature branch created)
- [ ] Issue understood completely
- [ ] Root cause identified (not just symptoms — no surface-level fixes)
- [ ] Solution approved before implementation
- [ ] **Failing test written BEFORE implementing fix**
- [ ] Changes are minimal and focused
- [ ] Edge cases enumerated and tested (3+ edge case tests)
- [ ] All tests passing (full suite, not just new tests)
- [ ] Self-review completed
- [ ] Commit message is clear and follows convention
- [ ] PR created and linked to issue
- [ ] Changelog updated at **repo root** (if project has CHANGELOG.md)
- [ ] Worktree cleaned up after merge
