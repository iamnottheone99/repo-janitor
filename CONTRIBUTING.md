# Contributing to Repo Janitor

Thank you for your interest in contributing to `repo-janitor`!

---

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/iamnottheone99/repo-janitor.git
   cd repo-janitor
   ```

2. **Install dependencies in editable mode**:
   ```bash
   pip install -e ".[dev]"
   ```

3. **Verify the test suite**:
   ```bash
   python -m pytest -v
   ```

---

## Architecture Principles

- **Zero-Pollution Guarantee**: Any autonomous modification must run inside an isolated Git worktree (`.janitor/worktrees/*`). The user's active working tree must never be dirtied or modified directly.
- **AXI Compliance**: CLI commands must support `--toon` output for AI agents, emitting minimal, machine-readable lines.
- **Single-Pass Classifiers**: Favor deterministic AST inspection, token shingles, and single-pass decision models (like Kev) over slow multi-round prompt engineering.

---

## Adding a New Language Adapter

To add support for a new language (e.g. C#, Java, Swift):
1. Subclass `LanguageAdapter` in `src/repo_janitor/languages.py`.
2. Define:
   - `file_extensions`: Target file suffixes.
   - `is_test_file`: Heuristic identifying test files.
   - `parse_symbols`: AST or regex parser extracting functions, methods, and classes.
   - `inject_docstring`: Formatter placing doc comments at the correct syntactic location.
   - `default_test_command`: Native test runner command.
3. Register your adapter in `ADAPTERS` and `detect_repo_language`.
4. Add corresponding tests in `tests/test_janitor.py`.

---

## Running the Quality Checks

Before submitting a pull request:
```bash
# Run unit & integration test suite
python -m pytest

# Run repo-janitor self-audit
janitor check --strict
```
