---
allowed-tools: Bash(git:*), Bash(gh:*)
argument-hint: <pr-number>
description: Review a PR to verify changelog alignment and assess merge safety
---

# Review PR Workflow

## Context

PR details:
!`gh pr view $ARGUMENTS`

Files changed:
!`gh pr diff $ARGUMENTS --name-only`

---

## Phase 0: Determine PR Ownership

**Check if this is your own PR (affects review type):**

```bash
PR_AUTHOR=$(gh pr view $ARGUMENTS --json author --jq '.author.login')
CURRENT_USER=$(gh api user --jq '.login')
echo "PR Author: $PR_AUTHOR | You: $CURRENT_USER"
```

**CRITICAL RULE:** If `PR_AUTHOR == CURRENT_USER`, you MUST use `--comment` review type (not `--request-changes` or `--approve`), because GitHub does not allow requesting changes or approving your own PR.

---

## Phase 1: Understand the PR

1. **Read the full PR description**
   ```bash
   gh pr view $ARGUMENTS --json title,body,labels,author,baseRefName,headRefName
   ```

2. **Review all commits**
   ```bash
   gh pr view $ARGUMENTS --json commits --jq '.commits[].messageHeadline'
   ```

3. **Identify the scope**
   - What is this PR trying to accomplish?
   - What type of change is it? (fix/feat/refactor/chore)
   - What areas of the codebase are affected?

---

## Phase 2: Review Code Changes

1. **Get the full diff**
   ```bash
   gh pr diff $ARGUMENTS
   ```

2. **List all modified files**
   ```bash
   gh pr diff $ARGUMENTS --name-only
   ```

3. **For each changed file, assess:**
   - What was modified?
   - Is the change consistent with the PR description?
   - Are there any changes not mentioned in the PR?

---

## Phase 3: Changelog Alignment

1. **Check if CHANGELOG.md was modified**
   ```bash
   gh pr diff $ARGUMENTS --name-only | grep -i changelog
   ```

2. **Read the changelog diff**
   ```bash
   gh pr diff $ARGUMENTS -- CHANGELOG.md
   ```

3. **Verify alignment:**

| Check | Status |
|-------|--------|
| **Changelog entry exists (if project has CHANGELOG.md)** | ⬜ |
| Entry matches PR type (Added/Changed/Fixed) | ⬜ |
| Entry accurately describes the change | ⬜ |
| Entry is not overstated or understated | ⬜ |
| No undocumented breaking changes | ⬜ |
| **No Claude/Anthropic attribution in commits or PR** | ⬜ |

> **Note:** Changelog is only required if the project already maintains a `CHANGELOG.md`. If none exists, skip this section.

4. **Common misalignments to catch:**
   - Code does more than changelog says
   - Code does less than changelog says
   - Changelog mentions features not in the diff
   - Breaking changes not flagged in changelog
   - Wrong category (e.g., "Fixed" when it's actually "Changed")

---

## Phase 4: Merge Safety Assessment

**As a meticulous reviewer, check for:**

### Code Quality
- [ ] No debug code (console.logs, print statements, TODOs)
- [ ] No commented-out code blocks
- [ ] No hardcoded secrets or credentials
- [ ] Error handling is present where needed
- [ ] Types are properly defined (no `any` escapes)

### Breaking Changes
- [ ] Function signatures unchanged OR documented as breaking
- [ ] API contracts unchanged OR documented as breaking
- [ ] Export names unchanged OR documented as breaking
- [ ] Database schema unchanged OR migration included
- [ ] Config format unchanged OR migration path documented

### Regression Risk
- [ ] Existing functionality preserved
- [ ] Edge cases handled
- [ ] Null/undefined checks where needed
- [ ] Tests added for new code paths
- [ ] Existing tests still relevant

### Dependencies
- [ ] No unnecessary new dependencies
- [ ] Dependency versions pinned appropriately
- [ ] No known vulnerabilities in new deps

### PR Size & Branch Freshness
- [ ] PR is not excessively large (flag if >500 lines changed — consider splitting)
- [ ] Branch is rebased on latest default branch (check for merge conflicts)

```bash
# Check PR size (additions + deletions)
gh pr diff $ARGUMENTS --stat | tail -1
```

---

## Phase 5: Verification

1. **Check CI/CD status (if configured)**
   ```bash
   gh pr checks $ARGUMENTS
   ```
   Note: Some repos may not have CI checks configured - skip if not applicable.

2. **Review test results**
   - All tests passing?
   - Any skipped tests?
   - Coverage maintained?

3. **Review any existing comments**
   ```bash
   gh pr view $ARGUMENTS --comments
   ```

---

## Phase 6: Final Report

**Provide a structured assessment:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 PR #$ARGUMENTS Review Summary
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Overview
- **Title:** [PR title]
- **Author:** [author]
- **Type:** [fix/feat/refactor/etc]
- **Files Changed:** [count]

## Changelog Alignment

[✅ ALIGNED / ⚠️ PARTIAL / ❌ MISALIGNED]

- [Details of alignment or issues found]

## Code Review Findings

### Issues Found
- [List any problems discovered]

### Observations
- [Notable patterns or concerns]

## Merge Safety

[✅ SAFE TO MERGE / ⚠️ MERGE WITH CAUTION / ❌ DO NOT MERGE]

**Risk Level:** [Low / Medium / High]

**Reasoning:**
- [Why it's safe or not safe]

## Recommendations

1. [Required action before merge, if any]
2. [Suggested improvements, if any]

## Verdict

[ ] ✅ Approve - Safe to merge
[ ] ⚠️ Request Changes - Issues must be addressed
[ ] 💬 Comment - Questions or suggestions only

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Phase 7: Create Issues for Findings (Optional)

**For non-blocking issues worth tracking, create GitHub issues instead of blocking the PR:**

```bash
# Verify label exists before using it
EXISTING_LABELS=$(gh label list --json name --jq '.[].name')

# Create issue for a finding
gh issue create \
  --title "[Review] <finding description>" \
  --body "Found during review of PR #$ARGUMENTS

## Finding
<description>

## Suggested Fix
<suggestion>" \
  --label "<label>"  # Only use labels that exist in EXISTING_LABELS
```

**IMPORTANT:** Before using any label in `gh issue create` or `gh pr edit`, verify it exists:
```bash
gh label list --json name --jq '.[].name' | grep -q "^<label>$" || echo "Label does not exist!"
```

---

## Quick Actions

```bash
# If this is YOUR OWN PR — use COMMENT (GitHub blocks approve/request-changes on own PRs)
gh pr review $ARGUMENTS --comment --body "Self-review: LGTM - changelog aligned, safe to merge"

# If reviewing SOMEONE ELSE's PR — approve or request changes
gh pr review $ARGUMENTS --approve --body "LGTM - changelog aligned, safe to merge"
gh pr review $ARGUMENTS --request-changes --body "See comments"

# Merge the PR (prefer rebase to maintain clean history)
gh pr merge $ARGUMENTS --rebase --delete-branch

# Fallback if rebase fails due to conflicts
gh pr merge $ARGUMENTS --squash --delete-branch
```
