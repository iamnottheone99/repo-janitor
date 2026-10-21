---
name: repo-janitor
description: "Audit and maintain repositories for Errors (syntax, broken imports, empty exception swallows), Gaps (unimplemented stubs, untested functions, missing docstrings), and Redundancies (code clones, dead code). Runs locally with zero tokens via fast decision classification or Ollama/Kev, emitting token-efficient TOON output or generating self-healed, verified fixes in isolated Git worktrees."
---

# repo-janitor

Autonomous repository auditor and hygiene maintainer for Python, TypeScript/JavaScript, Go, and Rust.

## When to Use

Use `repo-janitor` whenever you need to:
- Audit a codebase for **Errors** (syntax/parse failures, broken imports, empty exception swallows).
- Identify **Gaps** (unimplemented stubs, missing unit test coverage, missing docstrings).
- Identify **Redundancies** (Type-2/3 duplicate code clones, dead/uncalled symbols).
- Autonomously generate unit tests or docstrings inside **isolated Git worktrees** without polluting the user's active branch.

## Quick CLI Reference

Always append `--toon` when invoking in agent loops to keep output token-efficient and machine-readable.

### 1. Tri-Bucket Audit (Errors, Gaps, Redundancies)

Run directly from the root of the project you want to audit:

```powershell
# Inside any project root folder (defaults to current directory):
janitor check --toon

# Human-readable table view:
janitor check

# With Kev / System-One decision model:
janitor check --classifier kev --toon

# Target a different repository path explicitly:
janitor check --repo /path/to/repo --toon
```

### 2. Documentation & Test Gap Scan

```powershell
# Inside any project root:
janitor scan --toon
```

### 3. Autonomously Generate Passing Unit Tests in Isolated Worktrees

```powershell
# Inside any project root:
janitor tests --limit 3 --toon

# List verified branches ready for merge:
janitor branches --toon

# Prune janitor/* branches:
janitor prune
```

## TOON Schema

Audit responses adhere to Token-Oriented Object Notation:

```text
repo: <path>
summary: <N> errors, <N> gaps, <N> redundancies across <N> files
errors[<count>]{kind,file,line,conf,actionable,rec}:
  <kind>,<file>:<line>,<confidence>,<actionable>,<recommendation>
gaps[<count>]{kind,file,line,conf,actionable,rec}:
  <kind>,<file>:<line>,<confidence>,<actionable>,<recommendation>
redundancies[<count>]{kind,file,line,conf,actionable,rec}:
  <kind>,<file>:<line>,<confidence>,<actionable>,<recommendation>
```
