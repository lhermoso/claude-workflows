---
allowed-tools: Bash(git:*), Bash(wc:*), Bash(npm audit:*), Bash(cargo audit:*)
argument-hint: [directory] (optional - defaults to entire codebase)
description: Scan codebase for technical debt, code smells, and improvement opportunities
---

# Tech Debt Scanner

## Context

Repository root:
!`git rev-parse --show-toplevel`

Current branch:
!`git branch --show-current`

---

## Configuration

**Target directory:** `$ARGUMENTS` (if empty, scan entire repository)

**File types to scan:** Auto-detect from project structure. Common types: `.ts`, `.tsx`, `.js`, `.jsx`, `.rs`, `.py`, `.go`, `.java`, `.sol`, `.rb`, `.swift`, `.kt`, `.vue`, `.svelte`

> **Tip:** Use the Glob tool to discover which file types exist: `**/*.{ts,tsx,js,jsx,rs,py,go,java}` — then focus scanning on what's actually present.

---

## Phase 1: Codebase Overview

1. **Determine scan scope**
   - If `$ARGUMENTS` is provided, scan that directory
   - If empty, scan from repository root

2. **Get file statistics**
   Use the **Glob** tool with patterns like `**/*.{ts,tsx,js,jsx,rs,py}` to discover source files and count them.

3. **Identify largest files** (complexity indicator)
   Use Glob to find source files, then Read to check file sizes. Flag files over 500 lines.

---

## Phase 2: Explicit Debt Markers

Search for developer-left markers indicating known issues.

> **Use the native Grep tool** for all searches below (preferred over bash grep). It provides better output formatting and respects permissions.

### TODOs and FIXMEs
Search for patterns: `TODO`, `FIXME`, `XXX`, `HACK`

### Temporary/Workaround Code
Search for patterns (case-insensitive): `workaround`, `temporary`, `temp fix`, `quick fix`

### Deprecated Code
Search for patterns (case-insensitive): `@deprecated`, `DEPRECATED`

---

## Phase 3: Code Smells

Use the native **Grep** tool with the appropriate `glob` filter for each language:

### Type Safety Issues (TypeScript/JavaScript)
Search in `*.{ts,tsx}` files for:
- `: any` and `as any` — type escape hatches
- `as unknown` — type assertions
- `!.` — non-null assertions
- `@ts-ignore`, `@ts-nocheck`, `@ts-expect-error` — suppressed type errors

### Rust Specific
Search in `*.rs` files for:
- `.unwrap()` — potential panics
- `.expect("` — check if messages are descriptive
- `#[allow(` — suppressed lints
- `unsafe {` — unsafe blocks

### Rust Simulation/Config Verification
If the project contains simulation or optimizer code:
- Check that config file types are correctly matched to operations (simulation config vs optimizer config)
- Search for hardcoded TVL values, token bounds, and allocation ranges that should be configurable
- Flag any config loading that doesn't validate the config type matches the expected operation
- Search for: `config`, `Config`, `toml`, `json` in `*.rs` files to find config handling

### Python Specific
Search in `*.py` files for:
- `except:` — bare except (catches everything including KeyboardInterrupt)
- `# type: ignore` — suppressed type errors
- `# noqa` — suppressed linter warnings

---

## Phase 4: Debug & Development Artifacts

### Console/Print Statements
Use Grep to search for:
- JS/TS: `console.log`, `console.debug`, `console.info` in `*.{ts,tsx,js,jsx}`
- Rust: `println!`, `dbg!` in `*.rs`
- Python: `print(` in `*.py` (exclude test files)

### Commented Out Code
> **Note:** Don't flag individual comments — look for **3+ consecutive commented lines** which indicate dead code blocks. Use the Grep tool with multiline mode to find blocks of consecutive `//` or `#` comments.

### Disabled Tests
Use Grep to search for:
- `.skip`, `.only` (JS/TS test runners)
- `@pytest.mark.skip` (Python)
- `#[ignore]` (Rust)
- `@skip`, `@disabled` (Java/general)

---

## Phase 5: Architecture Smells

### Hardcoded Values
Use Grep to search for:
- Hardcoded URLs/IPs: `http://`, `https://`, IP address patterns
- Hardcoded credentials: `password\s*=`, `api_key\s*=`, `secret\s*=` (case-insensitive)
- Magic numbers: look for unexplained numeric constants like `3600`, `86400`, `1024`, `255`, etc.

### Tracked Secrets (CRITICAL)
```bash
# Check if .env files are tracked by git (should NEVER be)
git ls-files | grep -i '\.env'

# Check for common secret file patterns in tracked files
git ls-files | grep -iE '(credentials|secrets|\.key$|\.pem$|\.p12$)'
```

Use Grep to search for high-entropy strings and common secret patterns:
- `AKIA` (AWS access key prefix)
- `sk-` followed by alphanumeric (API keys)
- `ghp_`, `gho_`, `ghu_` (GitHub tokens)

### Long Files (>500 lines)
Use Glob to find source files, then check line counts. Flag any file over 500 lines.

### Deeply Nested Directories
Use Glob with deep patterns like `*/*/*/*/*/*/**.{ts,py,rs}` to find excessively nested files.

---

## Phase 6: Dependency Concerns

### Package.json (Node projects)
Use the **Read** tool to examine `package.json` dependencies section.

Look for:
- Outdated major versions
- Deprecated packages
- Duplicate functionality
- Security vulnerabilities: `npm audit` or `pnpm audit`

### Cargo.toml (Rust projects)
Use the **Read** tool to examine `Cargo.toml`.

Look for:
- Pinned versions without reason
- Git dependencies (instead of crates.io)
- Unused dependencies (`cargo audit` if available)

---

## Phase 7: Test Coverage Gaps

### Files without corresponding tests
Use Glob to find source files and test files, then compare:
- Source: `src/**/*.{ts,rs,py}`
- Tests: `**/*.{test,spec}.{ts,tsx}`, `**/*_test.rs`, `**/test_*.py`

Flag source files that have no corresponding test file.

### Test file analysis
Use Grep to count assertions per test file:
- JS/TS: `expect(`, `assert`
- Rust: `assert_eq!`, `assert!`
- Python: `assert`, `self.assert`

---

## Phase 8: Generate Report

**Output format:**

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍 TECH DEBT SCAN REPORT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📁 Scope: <directory or "entire repository">
📊 Files Scanned: <count>
📅 Date: <timestamp>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Summary

| Category | Count | Severity |
|----------|-------|----------|
| TODOs/FIXMEs | X | 🟡 Medium |
| Type Safety Issues | X | 🟠 High |
| Debug Artifacts | X | 🔴 Critical |
| Hardcoded Values | X | 🟠 High |
| Large Files (>500 loc) | X | 🟡 Medium |
| Disabled Tests | X | 🟠 High |
| Security Concerns | X | 🔴 Critical |

**Overall Debt Score:** [Low / Medium / High / Critical]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## 🔴 Critical Issues (Fix Immediately)

[List items that need immediate attention]
- File:Line - Description

## 🟠 High Priority (Plan to Fix)

[List items that should be addressed soon]
- File:Line - Description

## 🟡 Medium Priority (Track)

[List items to track and address when touching that code]
- File:Line - Description

## 🟢 Low Priority (Nice to Have)

[List minor improvements]
- File:Line - Description

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Detailed Findings

### Explicit Debt Markers
<list all TODOs, FIXMEs with file locations>

### Type Safety
<list all `any` types, assertions, ts-ignore>

### Debug Code
<list all console.logs, print statements>

### Architecture Issues
<list large files, complex nesting, hardcoded values>

### Test Gaps
<list files without tests, disabled tests>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Recommendations

1. **Immediate Actions**
   - [Specific action items]

2. **Short-term (This Sprint)**
   - [Action items]

3. **Long-term (Backlog)**
   - [Action items]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Severity Guidelines

| Severity | Criteria |
|----------|----------|
| 🔴 Critical | Security risks, production bugs, data loss potential |
| 🟠 High | Type safety holes, missing error handling, disabled tests |
| 🟡 Medium | TODOs, code smells, large files, missing docs |
| 🟢 Low | Style issues, minor refactors, nice-to-haves |

---

## Optional: Create GitHub Issues

After review, offer to create issues for significant findings:

```bash
gh issue create --title "[Tech Debt] <description>" --label "tech-debt" --body "<details>"
```
