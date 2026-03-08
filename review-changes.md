---
allowed-tools: Bash(git diff:*), Bash(git status:*), Bash(git log:*), Bash(git show:*)
description: Meticulous review of recent code changes to identify breaking changes and regressions
---

# Meticulous Code Review for Breaking Changes

## Context - Recent Changes

Current git status:
!`git status --short`

Default branch:
!`git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main"`

Current branch:
!`git branch --show-current`

Files changed (vs default branch or last commit):
!`git diff --name-only $(git merge-base HEAD $(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main")) 2>/dev/null || git diff --name-only HEAD~1 2>/dev/null || git diff --name-only --cached`

Recent commits on this branch:
!`git log --oneline $(git merge-base HEAD $(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || echo "main"))..HEAD 2>/dev/null || git log --oneline -5`

## Your Task

You are a **meticulous senior developer** performing a thorough code review. Your goal is to identify any changes that could break existing functionality.

### Review Process

1. **Gather the Full Diff**
   - Determine the diff range: use the merge-base against the default branch to capture ALL commits on this branch, not just the last one
   - Run `git diff $(git merge-base HEAD <default-branch>)` to see all branch changes
   - If on the default branch, fall back to `git diff HEAD~1` or `git diff HEAD` for uncommitted changes
   - For each modified file, understand its role in the codebase

2. **Analyze Each Changed File**
   For every modified file, check:
   
   - **Function Signatures**: Did any function parameters change? Are there missing default values?
   - **Return Types**: Did return types or structures change?
   - **Imports/Exports**: Were any exports removed or renamed?
   - **API Contracts**: Do any API endpoints have different request/response shapes?
   - **Database/State**: Are there schema changes or state mutations that could break existing data?
   - **Dependencies**: Were any dependencies added/removed/upgraded that could cause issues?

3. **Find Usages and Dependencies**
   - Search the codebase for all usages of modified functions/classes/types
   - Use Claude Code's native **Grep** tool to search across the codebase (preferred over bash grep)
   - Use **Glob** tool to find related files by pattern (preferred over bash find)
   - Use **Read** tool to examine file contents (preferred over bash cat)
   - Verify that all call sites are still compatible with the changes

4. **Check for Surface-Level vs Root Cause Fixes**

   **CRITICAL:** Flag fixes that appear to be surface-level workarounds:

   | Surface Fix (FLAG) | Likely Root Cause to Investigate |
   |--------------------|---------------------------------|
   | Added z-index | Layout stacking context / positioning issue |
   | Added overflow-hidden | Content sizing / negative margins |
   | Added retry/timeout | Race condition / missing await / wrong event |
   | Added null check without understanding why null | Data flow issue / missing initialization |
   | Added try/catch without handling cause | Upstream error propagation issue |

   If you spot a surface-level fix, flag it and suggest investigating the root cause.

5. **Check for Common Breaking Patterns**

   | Pattern | Risk |
   |---------|------|
   | Removed function/method | 🔴 High - will cause immediate errors |
   | Changed function signature | 🔴 High - callers may pass wrong args |
   | Changed return type/structure | 🟠 Medium - consumers may break |
   | Renamed exports | 🔴 High - importers will fail |
   | New required parameter without default | 🔴 High - existing calls break |
   | Changed error handling | 🟠 Medium - catch blocks may miss |
   | Async/sync change | 🔴 High - callers need await |
   | Type narrowing/widening | 🟡 Low-Medium - type errors possible |

6. **Frontend/Performance-Specific Checks**

   If changes involve HTML, CSS, or frontend code:
   - **LCP Candidates:** NEVER remove or significantly modify elements likely to be LCP candidates (h1, hero images, above-fold content) without checking their LCP status first. Removing an LCP element can crash Lighthouse scores.
   - **Layout Changes:** Verify that CSS changes don't break layouts on other viewports (mobile, tablet)
   - **Redirects/Middleware:** Flag any added redirects or middleware routes — these should only exist if explicitly requested

7. **CHANGELOG Path Verification**

   If CHANGELOG.md was modified, verify:
   ```bash
   # Ensure the correct CHANGELOG.md was updated (should be at repo root)
   find . -name "CHANGELOG.md" -not -path "*/node_modules/*" 2>/dev/null
   ```
   - Was the **repo root** CHANGELOG.md updated (not a nested one)?
   - Does the formatting match existing conventions (`[Unreleased]` section style, date formats)?
   - Was `[Unreleased]` preserved as-is (not renamed to `[Unversioned]` or removed)?

8. **Integration Points**
   - Check if changes affect any interfaces with external systems
   - Verify webhook handlers, API clients, database queries still match
   - Look for hardcoded values that may now be stale

9. **Test Coverage**
   - Identify if existing tests still pass with these changes
   - Note any tests that may need updating
   - Flag untested code paths

### Output Format

Provide your findings in this structure:

```
## Summary
[One paragraph overview of the changes and overall risk level]

## Breaking Changes Found
- [ ] **[HIGH/MEDIUM/LOW]** [File:Line] - Description of the issue
  - Impact: What will break
  - Fix: How to resolve

## Surface-Level Fix Warnings
- [ ] [File:Line] - Description of suspected surface fix
  - Symptom fix: What was done
  - Likely root cause: What should be investigated

## Potential Issues (Need Verification)
- [ ] [File:Line] - Description of concern
  - Why: Reasoning
  - Check: How to verify

## Frontend/Performance Concerns
- [ ] LCP candidate changes: [any h1/hero/above-fold modifications]
- [ ] CHANGELOG: [correct path, formatting preserved]

## Safe Changes
- [List changes that are confirmed safe]

## Recommendations
1. [Action items before merging/deploying]
```

### Begin Review

Start by examining the actual diff, then systematically work through each changed file. Be thorough - assume nothing is safe until verified.
