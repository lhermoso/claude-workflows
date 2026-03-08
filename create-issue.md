---
allowed-tools: Bash(git:*), Bash(gh:*)
argument-hint: <brief description of the issue>
description: Create a well-structured GitHub issue with clear acceptance criteria
---

# Create Issue Workflow

## Context

Current repository:
!`git remote get-url origin`

Current branch:
!`git branch --show-current`

Recent issues for reference:
!`gh issue list --limit 5`

Labels available:
!`gh label list --limit 20`

---

## Phase 1: Gather Information

Based on the input: **$ARGUMENTS**

Ask clarifying questions if needed:

1. **What type of issue is this?**
   - `bug` - Something isn't working
   - `feature` - New functionality request
   - `enhancement` - Improvement to existing functionality
   - `task` - Technical work item
   - `documentation` - Docs update needed

2. **What is the priority/severity?**
   - `critical` - System down, blocking all work
   - `high` - Major functionality broken, no workaround
   - `medium` - Feature broken but workaround exists
   - `low` - Minor issue, cosmetic, nice-to-have

3. **Any related issues or context?**

---

## Phase 2: Draft Issue

**Title format:** `[Type] Brief description`

Examples:
- `[Bug] Position sync fails when wallet has no active positions`
- `[Feature] Add volatility regime detection to hedge engine`
- `[Enhancement] Improve grid spacing algorithm for low liquidity pairs`

---

**Issue body template:**

```markdown
## Description

[Clear, concise description of the issue or feature request]
[What is happening vs what should happen]

## Context

[Why is this needed? What triggered this issue?]
[Any relevant background information]

## Steps to Reproduce (for bugs)

1. [First step]
2. [Second step]
3. [Expected result]
4. [Actual result]

## Acceptance Criteria

- [ ] [Specific, measurable outcome 1]
- [ ] [Specific, measurable outcome 2]
- [ ] [Specific, measurable outcome 3]
- [ ] [Tests pass / No regressions]

## Additional Information

- **Environment:** [OS, browser, version if relevant]
- **Related Issues:** #xxx
- **Screenshots/Logs:** [if applicable]
```

---

## Acceptance Criteria Guidelines

**Good acceptance criteria are:**

| ✅ Do | ❌ Don't |
|-------|----------|
| Specific and measurable | Vague or subjective |
| Testable (pass/fail) | Open to interpretation |
| User-focused outcomes | Implementation details |
| Independent items | Dependent on solutions |

**Examples:**

❌ Bad:
- Fix the bug
- Make it faster
- Improve the UX

✅ Good:
- Position sync completes without errors when wallet has zero positions
- Grid rebalancing executes in under 500ms for 50 price levels
- User can switch gender and see updated bodymap within 1 second

**Do NOT include:**
- Proposed solutions or implementation approaches
- Technical suggestions on how to fix
- Code snippets or file references
- "Co-authored by" or "Created by Claude" attributions

---

## Phase 3: Confirmation

**Present for review:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 Issue Preview
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Title: <title>

Labels: <labels>

<full description>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**⏸️ Wait for confirmation before creating issue**

---

## Phase 4: Create Issue

**IMPORTANT: Verify labels exist before using them.**

```bash
# First, check which labels exist in the repo
gh label list --json name --jq '.[].name'

# Only use labels that exist. If a label doesn't exist, either:
# 1. Create it first: gh label create "<label>" --color "FFFFFF"
# 2. Or omit it from the command
```

```bash
gh issue create \
  --title "<title>" \
  --body "<description>" \
  --label "<label1>,<label2>"   # Only use verified labels!
```

**Optional flags:**
```bash
--assignee "@me"           # Assign to yourself
--milestone "v1.0"         # Add to milestone
--project "Board Name"     # Add to project board
```

**Discover available milestones:**
```bash
gh api repos/:owner/:repo/milestones --jq '.[].title'
```

---

## Phase 5: Output

After creation, provide:

```
✅ Issue created successfully

Issue: #<number>
URL: <url>
Title: <title>
Labels: <labels>
```

---

## Quick Reference

```bash
# View issue
gh issue view <number>

# Edit issue
gh issue edit <number> --title "New title"

# Add labels
gh issue edit <number> --add-label "bug,high-priority"

# Close issue
gh issue close <number>

# Reopen issue
gh issue reopen <number>
```
