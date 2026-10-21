"""Autonomous maintenance orchestrator for docstrings, README, and unit tests."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from repo_janitor.config import JanitorConfig
from repo_janitor.languages import get_language_adapter
from repo_janitor.ollama_client import LLMGenerator, OllamaClient
from repo_janitor.scanner import CodeSymbol, RepoScanner, ScanReport
from repo_janitor.worktree import GitWorktreeManager


@dataclass
class TaskOutcome:
    """Result of a single maintenance task executed in an isolated worktree."""

    task_type: str  # "docstring" | "unit_test" | "readme"
    target: str
    file_path: str
    status: str  # "committed" | "failed_verification" | "skipped" | "error"
    branch: str = ""
    commit_sha: str = ""
    attempts: int = 0
    detail: str = ""


@dataclass
class MaintenanceSummary:
    """Summary of an autonomous maintenance session."""

    scan_report: ScanReport
    outcomes: list[TaskOutcome] = field(default_factory=list)

    @property
    def committed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "committed")

    @property
    def failed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("failed_verification", "error"))


def inject_docstring_into_source(
    source: str,
    symbol_name: str,
    qualified_name: str,
    docstring: str,
    language: str = "python",
) -> str:
    """Safely insert a docstring or doc comment into the target function, method, or class."""
    adapter = get_language_adapter(language)
    return adapter.inject_docstring(source, symbol_name, qualified_name, docstring)


class RepoMaintainer:
    """Orchestrates scanning, LLM generation, isolated worktrees, and test verification."""

    def __init__(
        self,
        repo_root: Path,
        config: JanitorConfig | None = None,
        llm: LLMGenerator | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or JanitorConfig.load(self.repo_root)
        self.scanner = RepoScanner(self.repo_root, self.config)
        self.llm: LLMGenerator = llm or OllamaClient(self.config)
        self.worktrees = GitWorktreeManager(self.repo_root, self.config)

    def scan(self) -> ScanReport:
        """Run a read-only AST & coverage scan of the repository."""
        return self.scanner.scan()

    def run_docstring_tasks(
        self, limit: int | None = None, include_readme: bool = True
    ) -> MaintenanceSummary:
        """Add missing docstrings (grouped per file) and update README in isolated worktrees."""
        report = self.scanner.scan()
        summary = MaintenanceSummary(scan_report=report)
        max_tasks = limit if limit is not None else self.config.max_tasks_per_run

        # Group missing docstrings by file so each file gets one clean branch/commit
        by_file: dict[str, list[CodeSymbol]] = {}
        for sym in report.missing_docstrings:
            by_file.setdefault(sym.file_path, []).append(sym)

        tasks_run = 0
        for rel_path, symbols in by_file.items():
            if tasks_run >= max_tasks:
                break
            tasks_run += 1
            outcome = self._process_file_docstrings(rel_path, symbols)
            summary.outcomes.append(outcome)

        if include_readme and report.readme_needs_update and tasks_run < max_tasks:
            summary.outcomes.append(self._process_readme_update(report))

        return summary

    def run_unit_test_tasks(self, limit: int | None = None) -> MaintenanceSummary:
        """Generate unit tests for uncovered functions in isolated worktrees with self-healing retries."""
        report = self.scanner.scan()
        summary = MaintenanceSummary(scan_report=report)
        max_tasks = limit if limit is not None else self.config.max_tasks_per_run

        for sym in report.untested_functions[:max_tasks]:
            outcome = self._process_unit_test(sym)
            summary.outcomes.append(outcome)

        return summary

    def run_all(self, limit: int | None = None) -> MaintenanceSummary:
        """Execute both docstring/README maintenance and unit test generation."""
        report = self.scanner.scan()
        summary = MaintenanceSummary(scan_report=report)
        max_tasks = limit if limit is not None else self.config.max_tasks_per_run

        doc_summary = self.run_docstring_tasks(limit=max_tasks, include_readme=True)
        summary.outcomes.extend(doc_summary.outcomes)

        remaining = max(0, max_tasks - len(summary.outcomes))
        if remaining > 0:
            test_summary = self.run_unit_test_tasks(limit=remaining)
            summary.outcomes.extend(test_summary.outcomes)

        return summary

    def _process_file_docstrings(
        self, rel_path: str, symbols: list[CodeSymbol]
    ) -> TaskOutcome:
        slug = Path(rel_path).stem
        try:
            with self.worktrees.isolated_worktree("docs", slug) as (wt_path, branch):
                target_file = wt_path / rel_path
                source = target_file.read_text(encoding="utf-8")

                documented_names: list[str] = []
                for sym in symbols:
                    doc = self.llm.generate_docstring(sym)
                    source = inject_docstring_into_source(
                        source,
                        sym.symbol_name,
                        sym.qualified_name,
                        doc,
                        language=sym.language,
                    )
                    documented_names.append(sym.qualified_name)

                target_file.write_text(source, encoding="utf-8")
                verification = self.worktrees.run_tests(wt_path)
                if not verification.passed:
                    return TaskOutcome(
                        task_type="docstring",
                        target=f"{rel_path} ({len(documented_names)} symbols)",
                        file_path=rel_path,
                        status="failed_verification",
                        branch=branch,
                        attempts=1,
                        detail=verification.combined_output[:400],
                    )

                sha = self.worktrees.commit_changes(
                    wt_path,
                    [rel_path],
                    f"docs({slug}): add missing docstrings for {', '.join(documented_names[:3])}",
                )
                return TaskOutcome(
                    task_type="docstring",
                    target=f"{rel_path} ({', '.join(documented_names)})",
                    file_path=rel_path,
                    status="committed",
                    branch=branch,
                    commit_sha=sha,
                    attempts=1,
                    detail=f"Added {len(documented_names)} docstring(s) and verified tests pass",
                )
        except Exception as exc:
            return TaskOutcome(
                task_type="docstring",
                target=rel_path,
                file_path=rel_path,
                status="error",
                detail=str(exc),
            )

    def _process_readme_update(self, report: ScanReport) -> TaskOutcome:
        try:
            with self.worktrees.isolated_worktree("docs", "readme-sync") as (
                wt_path,
                branch,
            ):
                readme_file = wt_path / "README.md"
                existing = (
                    readme_file.read_text(encoding="utf-8")
                    if readme_file.exists()
                    else f"# {self.repo_root.name}\n"
                )
                summaries: dict[str, str] = {}
                for src_file in self.scanner._discover_source_files():
                    rel = src_file.relative_to(self.repo_root).as_posix()
                    summaries[src_file.stem] = f"Source module at `{rel}`"

                updated_md = self.llm.generate_readme_section(
                    existing, report.readme_missing_modules, summaries
                )
                readme_file.write_text(updated_md.strip() + "\n", encoding="utf-8")

                verification = self.worktrees.run_tests(wt_path)
                if not verification.passed:
                    return TaskOutcome(
                        task_type="readme",
                        target="README.md",
                        file_path="README.md",
                        status="failed_verification",
                        branch=branch,
                        attempts=1,
                        detail=verification.combined_output[:400],
                    )

                sha = self.worktrees.commit_changes(
                    wt_path,
                    ["README.md"],
                    f"docs(readme): document modules {', '.join(report.readme_missing_modules[:3])}",
                )
                return TaskOutcome(
                    task_type="readme",
                    target="README.md",
                    file_path="README.md",
                    status="committed",
                    branch=branch,
                    commit_sha=sha,
                    attempts=1,
                    detail=f"Updated README.md with {len(report.readme_missing_modules)} module(s)",
                )
        except Exception as exc:
            return TaskOutcome(
                task_type="readme",
                target="README.md",
                file_path="README.md",
                status="error",
                detail=str(exc),
            )

    def _process_unit_test(self, sym: CodeSymbol) -> TaskOutcome:
        adapter = get_language_adapter(sym.language)
        module_import = adapter.derive_import_target(sym.file_path)
        slug = f"{Path(sym.file_path).stem}-{sym.qualified_name.replace('.', '-')}"
        test_rel_path = adapter.generated_test_rel_path(sym, slug, self.config.tests_dir)

        try:
            with self.worktrees.isolated_worktree("tests", slug) as (wt_path, branch):
                full_source = (wt_path / sym.file_path).read_text(encoding="utf-8")
                test_file_path = wt_path / test_rel_path
                test_file_path.parent.mkdir(parents=True, exist_ok=True)

                last_error: str | None = None
                max_attempts = max(1, self.config.max_retries + 1)

                for attempt in range(1, max_attempts + 1):
                    test_code = self.llm.generate_unit_test(
                        symbol=sym,
                        module_import_path=module_import,
                        full_file_source=full_source,
                        previous_error=last_error,
                    )
                    test_file_path.write_text(test_code.strip() + "\n", encoding="utf-8")

                    verification = self.worktrees.run_tests(wt_path)
                    if verification.passed:
                        sha = self.worktrees.commit_changes(
                            wt_path,
                            [test_rel_path],
                            f"test({Path(sym.file_path).stem}): add unit tests for {sym.qualified_name}",
                        )
                        return TaskOutcome(
                            task_type="unit_test",
                            target=sym.qualified_name,
                            file_path=test_rel_path,
                            status="committed",
                            branch=branch,
                            commit_sha=sha,
                            attempts=attempt,
                            detail=f"Generated passing {adapter.test_framework} suite in {test_rel_path}",
                        )
                    last_error = verification.combined_output

                # All attempts failed; remove the unverified test file so branch is discarded cleanly
                if test_file_path.exists():
                    test_file_path.unlink()
                return TaskOutcome(
                    task_type="unit_test",
                    target=sym.qualified_name,
                    file_path=test_rel_path,
                    status="failed_verification",
                    branch=branch,
                    attempts=max_attempts,
                    detail=(last_error or "Tests failed")[:400],
                )
        except Exception as exc:
            return TaskOutcome(
                task_type="unit_test",
                target=sym.qualified_name,
                file_path=test_rel_path,
                status="error",
                detail=str(exc),
            )

    @staticmethod
    def _derive_module_import(rel_path: str) -> str:
        """Convert `src/pkg/mod.py` or `pkg/mod.py` into `pkg.mod`."""
        return get_language_adapter("python").derive_import_target(rel_path)
