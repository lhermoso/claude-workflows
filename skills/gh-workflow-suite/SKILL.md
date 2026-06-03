---
name: gh-workflow-suite
description: Codex-native port of the `claude-workflows` Git and GitHub automation suite. Use when the user asks to run or emulate `/commit`, `/pr`, `/fix-issue`, `/quick-fix`, `/create-issue`, `/review-pr`, `/review-changes`, `/scan-debt`, `/batch-issues`, `/drain-issues`, `/issue-pipeline`, or `/full-review`, or when they want an end-to-end issue-to-PR workflow, changelog-aware PR creation, backlog draining, or iterative Codex review loops.
---

# GH Workflow Suite

## Overview

Use this skill to preserve the intent of the original Claude slash commands while adapting them to Codex's actual primitives: skills, `exec_command`, `apply_patch`, `update_plan`, GitHub app tools, `gh`, and `codex review`.

Read [references/porting-notes.md](references/porting-notes.md) at the start of the task, then open the matching workflow section in [references/workflows.md](references/workflows.md).

## Workflow Selection

- `/commit` -> `Commit`
- `/pr` -> `Pull Request`
- `/fix-issue` -> `Fix Issue`
- `/quick-fix` -> `Quick Fix`
- `/create-issue` -> `Create Issue`
- `/review-pr` -> `Review PR`
- `/review-changes` -> `Review Changes`
- `/scan-debt` -> `Scan Debt`
- `/batch-issues` -> `Batch Issues`
- `/drain-issues` -> `Drain Issues`
- `/issue-pipeline` -> `Issue Pipeline`
- `/full-review` -> `Full Review`

## Operating Rules

- Prefer GitHub plugin tools for issue and PR metadata, diffs, labels, comments, and PR creation. Use `gh` for checkout, worktrees, branch management, pushes, and operations the plugin does not expose cleanly.
- Prefer `codex review` or `codex exec review` for review-centric flows. To capture the final review text as a file, use `-o`/`--output-last-message` — but note it is supported by `codex exec review`, not by the bare `codex review` alias. Never pass `--full-auto`/`-s`/`-a` to either review form: the review subcommand rejects them and runs read-only + non-interactive by default.
- Use `update_plan` for multi-step work. Pause for approval only when the user explicitly asked for a checkpoint or when risk or ambiguity is materially high.
- Only use `spawn_agent` when the user explicitly asks for delegation, sub-agents, or parallel agent work. Otherwise run the workflow sequentially in the main thread.
- Prefer `git worktree` for issue flows when the sandbox allows writing adjacent directories. If it does not, stay in the current workspace on a dedicated branch and call out the fallback.
- Keep findings-first output for all review workflows, ordered by severity with concrete file references.

## References

- [references/porting-notes.md](references/porting-notes.md): Claude-to-Codex differences, tool mapping, and invocation examples.
- [references/workflows.md](references/workflows.md): The workflow definitions to follow for each former slash command.
