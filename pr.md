---
allowed-tools: Bash(git:*), Bash(gh:*)
description: Create a detailed pull request with proper documentation and changelog update
---

# Pull Request Workflow

## Context

Default branch:
!`git remote show origin | grep 'HEAD branch' | cut -d: -f2 | tr -d ' '`

Current branch:
!`git branch --show-current`

Commits to be included:
!`git log origin/main..HEAD --oneline 2>/dev/null || git log origin/master..HEAD --oneline`

Files changed:
!`git diff origin/main --name-status 2>/dev/null || git diff origin/master --name-status`

---

## Phase 0: Branch Safety Check

**CRITICAL: Verify you are NOT on master/main.**

```bash
CURRENT_BRANCH=$(git branch --show-current)
DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")

if [ "$CURRENT_BRANCH" = "$DEFAULT_BRANCH" ] || [ "$CURRENT_BRANCH" = "master" ] || [ "$CURRENT_BRANCH" = "main" ]; then
  echo "⛔ ERROR: You are on $CURRENT_BRANCH. Create a feature branch first!"
  echo "Run: git checkout -b <feature-branch-name>"
  exit 1
fi
```

**If on master/main:** STOP. Create a feature branch first. NEVER push directly to the default branch.

---

## Phase 1: Review Changes

1. **Verify all changes are committed**
   ```bash
   git status
   ```

2. **Ensure branch is up to date**
   ```bash
   git fetch origin
   # Detect default branch and rebase
   DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")
   git rebase origin/$DEFAULT_BRANCH
   ```

   **If rebase has conflicts:**
   - Review conflicts with `git diff`
   - Resolve each conflict manually
   - `git add <resolved-files>` then `git rebase --continue`
   - If rebase is too complex, abort with `git rebase --abort` and ask for guidance

3. **Run `/review-changes` for thorough code review**
   ```
   /review-changes
   ```

   This will check for:
   - Breaking changes
   - Debug code removal
   - Type safety issues
   - Test coverage
   - Regressions

---

## Phase 2: Craft PR Description

**Create a professional, detailed description following this structure:**

```markdown
## Summary

[2-3 sentence overview of what this PR accomplishes]

## Problem

[What issue/need does this address?]
- Bullet points describing the problem
- Include symptoms users experienced
- Reference issue numbers if applicable

## Root Cause Analysis

[For bug fixes - what was actually wrong?]
- Technical explanation of the underlying issue
- Why did this happen?

## Solution Implemented

### 1. [First Major Change]
**File:** `path/to/file.ts`
- What was changed
- Why this approach was chosen

### 2. [Second Major Change]
**File:** `path/to/file.ts`
- What was changed
- Why this approach was chosen

[Continue for each significant change]

## Technical Details

[Optional - for complex changes]
- Architecture decisions
- Data flow explanations
- Performance considerations

## Testing Performed

- ✅ [Test case 1]
- ✅ [Test case 2]
- ✅ [Test case 3]
- ✅ TypeScript compilation passes
- ✅ Linting shows no warnings

## Impact

[What does this fix/enable for users?]
- User-facing improvements
- Developer experience improvements

## Breaking Changes

[None / List any breaking changes and migration steps]

## Files Modified

- `path/to/file1.ts` - [brief description]
- `path/to/file2.ts` - [brief description]
```

---

## Phase 3: Create PR Title

**Format:** `<type>(<scope>): <description>`

| Type | Use for |
|------|---------|
| `fix` | Bug fixes |
| `feat` | New features |
| `refactor` | Code restructuring |
| `perf` | Performance improvements |
| `docs` | Documentation |
| `chore` | Maintenance |

**Examples:**
- `fix(workout): sync gender state across bodymap and videos`
- `feat(grid-bot): add volatility regime detection`
- `refactor(api): consolidate position endpoints`

---

## Phase 4: Confirmation

**Present for review:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 Pull Request Preview
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Title: <title>

Branch: <current> → <default branch>

<full description>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**⏸️ Wait for confirmation before creating PR**

---

## Phase 5: Create Pull Request

```bash
git push -u origin HEAD

gh pr create \
  --title "<title>" \
  --body "<description>"
```

**Capture the PR URL for changelog entry.**

---

## Phase 6: Changelog Update

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

## Quick Reference

```bash
# View PR in browser
gh pr view --web

# Create as draft PR (for WIP changes)
gh pr create --draft --title "<title>" --body "<body>"

# Mark draft as ready for review
gh pr ready

# Add reviewers
gh pr edit --add-reviewer username

# Convert back to draft
gh pr ready --undo

# Merge when ready (prefer rebase for clean history)
gh pr merge --rebase --delete-branch

# Fallback if rebase fails
gh pr merge --squash --delete-branch
```

---

## Changelog Path Verification

**IMPORTANT:** Before modifying CHANGELOG.md, verify you're editing the correct one:

```bash
# Check all CHANGELOG locations in the repo
find . -name "CHANGELOG.md" -not -path "*/node_modules/*" 2>/dev/null
```

Always update the CHANGELOG.md at the **repository root**. Follow existing formatting conventions exactly (e.g., `[Unreleased]` section style, date formats).

---

## Checklist

- [ ] **NOT on master/main** (feature branch created)
- [ ] All changes committed
- [ ] Branch rebased on latest default branch (main/master)
- [ ] Tests passing
- [ ] PR title follows conventional format
- [ ] Description explains what AND why
- [ ] Testing section documents verification
- [ ] Breaking changes noted (if any)
- [ ] Changelog updated at **repo root** (MANDATORY)
