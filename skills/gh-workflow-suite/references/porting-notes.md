# Porting Notes

## Purpose

This skill ports the original `claude-workflows` command set to Codex without pretending there is a one-to-one slash-command runtime. In this local Codex install, skills are the reusable discovery mechanism.

## Core Differences

- Claude slash-command frontmatter such as `allowed-tools`, `argument-hint`, and inline `!` shell snippets do not map directly. Preserve behavior, not syntax.
- Codex does not expose a local slash-command directory in this install. Invoke the port as a skill, for example: `Use $gh-workflow-suite to run the /fix-issue flow for issue #42.`
- Claude `Task` maps to `spawn_agent`, but only when the user explicitly requested delegation, sub-agents, or parallel agent work.
- Claude plan mode maps to `update_plan` plus normal commentary. Do not stop for plan approval unless the user asked for it or the risk is high.
- Claude `Read`, `Grep`, and `Glob` map to `exec_command` and `multi_tool_use.parallel`, typically using `rg`, `sed`, `git`, and other local CLI tools.
- Claude `Edit` and `Write` map to `apply_patch` for manual edits.

## Tool Mapping

| Claude workflow concept | Codex-native equivalent |
| --- | --- |
| Slash command | Explicit skill invocation or natural-language request |
| `Bash(...)` | `exec_command` |
| `Read` / `Grep` / `Glob` | `exec_command` with `rg`, `sed`, `find`, `git`; parallelize independent reads with `multi_tool_use.parallel` |
| `Edit` / `Write` | `apply_patch` |
| `Task` subagent | `spawn_agent` only with explicit user permission |
| Plan mode | `update_plan` and concise commentary |
| Claude self-review | `codex review` or findings-first manual review |
| Codex subprocess parsing | Prefer `codex exec ... -o <file>` over JSONL parsing |

## GitHub Guidance

- Prefer the GitHub plugin for fetching issues, PR metadata, diffs, comments, and creating PRs.
- Prefer `gh` for repo-local operations: `gh pr checkout`, `gh issue view`, `gh pr diff`, branch pushes, and any flow that is already shell-centric.
- If the user asks to fix CI specifically, prefer the existing `github:gh-fix-ci` skill instead of reproducing that logic here.
- If the user asks to address review comments on an existing PR, prefer `github:gh-address-comments` when it covers the task.

## Review Guidance

- Use `codex review --base origin/<default-branch>` for branch reviews and `codex review --uncommitted` for local changes when a fresh review pass is useful.
- Do not depend exclusively on `[P1]`/`[P2]`/`[P3]` tags. If the review output uses those tags, respect them. If it uses prose severity, treat any clearly blocking or high-severity finding as blocking.
- Keep review responses findings-first with file references.

## Worktree Guidance

- Prefer worktrees for issue workflows because they preserve isolation.
- If the sandbox or workspace policy prevents writing sibling directories, fall back to a dedicated branch in the current workspace and state that the workflow is using the fallback.

## Invocation Examples

- `Use $gh-workflow-suite to run the /commit flow for the current changes.`
- `Use $gh-workflow-suite to do the /fix-issue workflow for issue #42.`
- `Use $gh-workflow-suite to emulate /issue-pipeline for "add volatility regime detection" with --plan-review.`
- `Use $gh-workflow-suite to run /review-pr for PR #123.`
- `Use $gh-workflow-suite to perform /drain-issues label:bug with --dry-run.`
