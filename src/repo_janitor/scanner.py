"""AST & coverage scanner for identifying missing docstrings and untested functions."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from repo_janitor.config import JanitorConfig


@dataclass
class CodeSymbol:
    """Represents a parsed function, method, or class in the repository."""

    file_path: str
    symbol_name: str
    qualified_name: str
    kind: str  # "function" | "method" | "class"
    start_line: int
    end_line: int
    body_start_line: int
    signature: str
    has_docstring: bool
    is_tested: bool
    source_code: str


@dataclass
class ScanReport:
    """Aggregated findings from scanning a repository."""

    repo_root: Path
    total_files: int = 0
    total_symbols: int = 0
    missing_docstrings: list[CodeSymbol] = field(default_factory=list)
    untested_functions: list[CodeSymbol] = field(default_factory=list)
    readme_needs_update: bool = False
    readme_missing_modules: list[str] = field(default_factory=list)


class RepoScanner:
    """Scans Python files via AST (with optional coverage.json integration)."""

    def __init__(self, repo_root: Path, config: JanitorConfig | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or JanitorConfig.load(self.repo_root)

    def scan(self) -> ScanReport:
        """Scan the repository for missing docstrings and uncovered functions."""
        report = ScanReport(repo_root=self.repo_root)
        py_files = self._discover_source_files()
        test_corpus = self._load_test_corpus()
        coverage_map = self._load_coverage_map()

        report.total_files = len(py_files)
        documented_modules: list[str] = []

        for py_file in py_files:
            rel_path = py_file.relative_to(self.repo_root).as_posix()
            documented_modules.append(py_file.stem)
            symbols = self._parse_file(py_file, rel_path, test_corpus, coverage_map)
            for sym in symbols:
                report.total_symbols += 1
                if not sym.has_docstring:
                    report.missing_docstrings.append(sym)
                if sym.kind in ("function", "method") and not sym.is_tested:
                    if not sym.symbol_name.startswith("__"):
                        report.untested_functions.append(sym)

        readme_path = self.repo_root / "README.md"
        if not readme_path.exists():
            report.readme_needs_update = True
            report.readme_missing_modules = [
                m for m in documented_modules if m != "__init__"
            ]
        else:
            readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
            missing_mods = [
                m
                for m in documented_modules
                if m != "__init__" and m not in readme_text
            ]
            if missing_mods:
                report.readme_needs_update = True
                report.readme_missing_modules = missing_mods

        return report

    def _discover_source_files(self) -> list[Path]:
        """Find all non-test Python files inside configured source directories."""
        found: list[Path] = []
        seen: set[Path] = set()

        for src_dir_name in self.config.source_dirs:
            base = (self.repo_root / src_dir_name).resolve()
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.py")):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                rel_parts = resolved.relative_to(self.repo_root).parts
                if any(part in self.config.exclude_dirs for part in rel_parts):
                    continue
                if self.config.tests_dir in rel_parts or path.name.startswith("test_"):
                    continue
                seen.add(resolved)
                found.append(resolved)
        return found

    def _load_test_corpus(self) -> str:
        """Read all test files into a combined string to check symbol references."""
        tests_root = self.repo_root / self.config.tests_dir
        chunks: list[str] = []
        if tests_root.exists():
            for test_file in sorted(tests_root.rglob("*.py")):
                try:
                    chunks.append(test_file.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
        return "\n".join(chunks)

    def _load_coverage_map(self) -> dict[str, set[int]]:
        """Load line execution data from coverage.json if available."""
        cov_file = self.repo_root / "coverage.json"
        if not cov_file.exists():
            return {}
        try:
            data = json.loads(cov_file.read_text(encoding="utf-8"))
            files_data = data.get("files", {})
            result: dict[str, set[int]] = {}
            for fpath, info in files_data.items():
                norm = Path(fpath).as_posix()
                executed = set(info.get("executed_lines", []))
                result[norm] = executed
            return result
        except Exception:
            return {}

    def _parse_file(
        self,
        file_path: Path,
        rel_path: str,
        test_corpus: str,
        coverage_map: dict[str, set[int]],
    ) -> list[CodeSymbol]:
        """Parse a single Python file and extract functions, methods, and classes."""
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            return []

        lines = source.splitlines()
        symbols: list[CodeSymbol] = []

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym = self._build_symbol(
                    node=node,
                    kind="function",
                    qualified_name=node.name,
                    rel_path=rel_path,
                    lines=lines,
                    test_corpus=test_corpus,
                    coverage_map=coverage_map,
                )
                if sym:
                    symbols.append(sym)
            elif isinstance(node, ast.ClassDef):
                cls_sym = self._build_symbol(
                    node=node,
                    kind="class",
                    qualified_name=node.name,
                    rel_path=rel_path,
                    lines=lines,
                    test_corpus=test_corpus,
                    coverage_map=coverage_map,
                )
                if cls_sym:
                    symbols.append(cls_sym)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_sym = self._build_symbol(
                            node=item,
                            kind="method",
                            qualified_name=f"{node.name}.{item.name}",
                            rel_path=rel_path,
                            lines=lines,
                            test_corpus=test_corpus,
                            coverage_map=coverage_map,
                        )
                        if method_sym:
                            symbols.append(method_sym)

        return symbols

    def _build_symbol(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        kind: str,
        qualified_name: str,
        rel_path: str,
        lines: list[str],
        test_corpus: str,
        coverage_map: dict[str, set[int]],
    ) -> CodeSymbol | None:
        if not self.config.include_private_symbols and node.name.startswith("_") and node.name != "__init__":
            return None

        start_line = node.lineno
        end_line = getattr(node, "end_lineno", start_line) or start_line
        body_start_line = node.body[0].lineno if node.body else start_line + 1
        has_doc = ast.get_docstring(node) is not None
        snippet = "\n".join(lines[start_line - 1 : end_line])
        sig_line = lines[start_line - 1].strip()

        # Check coverage.json first if present, otherwise fallback to test corpus symbol lookup
        is_tested = False
        if coverage_map:
            executed = coverage_map.get(rel_path, set())
            body_lines = set(range(body_start_line, end_line + 1))
            is_tested = bool(executed & body_lines)
        else:
            is_tested = (
                f"test_{node.name}" in test_corpus
                or f"{node.name}(" in test_corpus
                or f".{node.name}(" in test_corpus
            )

        return CodeSymbol(
            file_path=rel_path,
            symbol_name=node.name,
            qualified_name=qualified_name,
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            body_start_line=body_start_line,
            signature=sig_line,
            has_docstring=has_doc,
            is_tested=is_tested,
            source_code=snippet,
        )
