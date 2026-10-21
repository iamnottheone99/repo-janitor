"""Multi-language AST & coverage scanner for identifying missing docstrings and untested functions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from repo_janitor.classifier import (
    DecisionClassifier,
    FastDecisionClassifier,
    Finding,
    KevDecisionClassifier,
    compute_token_fingerprint,
    jaccard_similarity,
    tokenize_code,
)
from repo_janitor.config import JanitorConfig
from repo_janitor.languages import (
    EXTENSION_TO_ADAPTER,
    LanguageAdapter,
    get_adapter_for_path,
    get_language_adapter,
)


@dataclass
class CodeSymbol:
    """Represents a parsed function, method, or class/struct in the repository."""

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
    language: str = "python"


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


@dataclass
class AuditReport:
    """Tri-bucket repository audit report (Errors, Gaps, and Redundancies)."""

    repo_root: Path
    total_files: int = 0
    total_symbols: int = 0
    errors: list[Finding] = field(default_factory=list)
    gaps: list[Finding] = field(default_factory=list)
    redundancies: list[Finding] = field(default_factory=list)

    @property
    def total_findings(self) -> int:
        return len(self.errors) + len(self.gaps) + len(self.redundancies)


class RepoScanner:
    """Scans source files across languages (with coverage.json, lcov.info, and cover.out integration)."""

    def __init__(self, repo_root: Path | str, config: JanitorConfig | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.config = config or JanitorConfig.load(self.repo_root)

    def _allowed_extensions(self) -> tuple[str, ...]:
        if self.config.language and self.config.language.lower() != "auto":
            adapter = get_language_adapter(self.config.language)
            return adapter.file_extensions
        return tuple(EXTENSION_TO_ADAPTER.keys())

    def scan(self) -> ScanReport:
        """Scan the repository for missing docstrings and uncovered functions."""
        report = ScanReport(repo_root=self.repo_root)
        source_files = self._discover_source_files()
        test_corpus = self._load_test_corpus()
        coverage_map = self._load_coverage_map()

        report.total_files = len(source_files)
        documented_modules: list[str] = []

        for src_file in source_files:
            rel_path = src_file.relative_to(self.repo_root).as_posix()
            documented_modules.append(src_file.stem)
            symbols = self._parse_file(src_file, rel_path, test_corpus, coverage_map)
            for sym in symbols:
                report.total_symbols += 1
                if not sym.has_docstring:
                    report.missing_docstrings.append(sym)
                if sym.kind in ("function", "method") and not sym.is_tested:
                    if not sym.symbol_name.startswith("__"):
                        report.untested_functions.append(sym)

        readme_path = self.repo_root / "README.md"
        ignored_stems = {"__init__", "index", "mod", "lib", "main"}
        if not readme_path.exists():
            report.readme_needs_update = True
            report.readme_missing_modules = [
                m for m in documented_modules if m not in ignored_stems
            ]
        else:
            readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
            missing_mods = [
                m
                for m in documented_modules
                if m not in ignored_stems and m not in readme_text
            ]
            if missing_mods:
                report.readme_needs_update = True
                report.readme_missing_modules = missing_mods

        return report

    def audit(self, classifier: DecisionClassifier | None = None) -> AuditReport:
        """Run tri-bucket repository audit identifying Errors, Gaps, and Redundancies."""
        import ast
        import re

        if classifier is None:
            if getattr(self.config, "classifier_model", "fast") in ("kev", "ollama"):
                classifier = KevDecisionClassifier(
                    endpoint=f"{self.config.ollama_host.rstrip('/')}/api/chat",
                    model=self.config.model,
                )
            else:
                classifier = FastDecisionClassifier()

        audit_rep = AuditReport(repo_root=self.repo_root)
        source_files = self._discover_source_files()
        test_corpus = self._load_test_corpus()
        coverage_map = self._load_coverage_map()
        audit_rep.total_files = len(source_files)

        all_symbols: list[CodeSymbol] = []
        file_sources: dict[str, str] = {}

        # 1. Errors: syntax errors, empty exception handlers, broken imports
        for src_file in source_files:
            rel_path = src_file.relative_to(self.repo_root).as_posix()
            try:
                raw_text = src_file.read_text(encoding="utf-8")
                file_sources[rel_path] = raw_text
            except Exception as exc:
                is_act, conf, rec = classifier.classify_finding("error", "syntax_error", rel_path, src_file.name, str(exc))
                audit_rep.errors.append(
                    Finding("error", "syntax_error", f"Unreadable file: {src_file.name}", rel_path, 1, conf, is_act, rec)
                )
                continue

            if src_file.suffix == ".py":
                try:
                    tree = ast.parse(raw_text, filename=rel_path)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.ExceptHandler):
                            body_stmts = getattr(node, "body", [])
                            if body_stmts and len(body_stmts) == 1:
                                stmt = body_stmts[0]
                                if isinstance(stmt, ast.Pass) or (
                                    isinstance(stmt, ast.Expr)
                                    and isinstance(getattr(stmt, "value", None), ast.Constant)
                                    and getattr(stmt.value, "value", None) == ...
                                ):
                                    is_act, conf, rec = classifier.classify_finding(
                                        "error", "empty_exception", rel_path, "except", "except: pass"
                                    )
                                    audit_rep.errors.append(
                                        Finding("error", "empty_exception", "Silent empty exception handler", rel_path, getattr(node, "lineno", 1), conf, is_act, rec)
                                    )
                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                top_pkg = node.module.split(".")[0]
                                known_roots = {p.stem for s in self.config.source_dirs for p in (self.repo_root / s).glob("*.py")}
                                known_roots.update({p.name for s in self.config.source_dirs for p in (self.repo_root / s).glob("*/")})
                                if top_pkg in known_roots:
                                    mod_path = "/".join(node.module.split(".")) + ".py"
                                    init_path = "/".join(node.module.split(".")) + "/__init__.py"
                                    found = any(
                                        (self.repo_root / s / mod_path).exists()
                                        or (self.repo_root / s / init_path).exists()
                                        for s in self.config.source_dirs
                                    )
                                    if not found:
                                        is_act, conf, rec = classifier.classify_finding(
                                            "error", "broken_import", rel_path, node.module, f"from {node.module} import ..."
                                        )
                                        audit_rep.errors.append(
                                            Finding("error", "broken_import", f"Broken import target: `{node.module}`", rel_path, node.lineno, conf, is_act, rec)
                                        )
                except SyntaxError as syn_err:
                    is_act, conf, rec = classifier.classify_finding("error", "syntax_error", rel_path, src_file.name, str(syn_err))
                    audit_rep.errors.append(
                        Finding("error", "syntax_error", f"Syntax error: {syn_err.msg}", rel_path, syn_err.lineno or 1, conf, is_act, rec)
                    )
                    continue
                except Exception as exc:
                    is_act, conf, rec = classifier.classify_finding("error", "syntax_error", rel_path, src_file.name, str(exc))
                    audit_rep.errors.append(
                        Finding("error", "syntax_error", f"Parse error: {exc}", rel_path, 1, conf, is_act, rec)
                    )
                    continue

            symbols = self._parse_file(src_file, rel_path, test_corpus, coverage_map)
            all_symbols.extend(symbols)

        audit_rep.total_symbols = len(all_symbols)

        # 2. Gaps: unimplemented stubs, untested functions, missing docstrings
        for sym in all_symbols:
            clean_body = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", sym.source_code).strip()
            lines = [l.strip() for l in clean_body.splitlines() if l.strip() and not l.strip().startswith(("def ", "func ", "export ", "pub ", "@"))]
            is_stub = len(lines) == 1 and any(
                lines[0] == s or lines[0].startswith("raise NotImplementedError") for s in ("pass", "...", "{ }", "{}")
            )
            if is_stub:
                is_act, conf, rec = classifier.classify_finding("gap", "stub_function", sym.file_path, sym.qualified_name, sym.source_code)
                audit_rep.gaps.append(
                    Finding("gap", "stub_function", f"Unimplemented stub: `{sym.qualified_name}`", sym.file_path, sym.start_line, conf, is_act, rec)
                )

            if sym.kind in ("function", "method") and not sym.is_tested and not sym.symbol_name.startswith("_"):
                is_act, conf, rec = classifier.classify_finding("gap", "untested_function", sym.file_path, sym.qualified_name, sym.signature)
                audit_rep.gaps.append(
                    Finding("gap", "untested_function", f"Untested function: `{sym.qualified_name}`", sym.file_path, sym.start_line, conf, is_act, rec)
                )

        # 3. Redundancies: Code clones & dead code
        min_lines = getattr(self.config, "min_clone_lines", 3)
        thresh = getattr(self.config, "similarity_threshold", 0.85)
        candidates = [
            s for s in all_symbols
            if (s.end_line - s.start_line + 1) >= min_lines
            and s.kind in ("function", "method")
            and not s.source_code.strip().endswith("...")
            and not s.source_code.strip().endswith("pass")
        ]
        fingerprints = [compute_token_fingerprint(tokenize_code(s.source_code)) for s in candidates]

        reported_clones: set[tuple[str, str]] = set()
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                sym_a, sym_b = candidates[i], candidates[j]
                if sym_a.file_path == sym_b.file_path and sym_a.symbol_name == sym_b.symbol_name:
                    continue
                if sym_a.symbol_name == "__init__" or sym_b.symbol_name == "__init__":
                    continue
                pair_key = tuple(sorted([f"{sym_a.file_path}:{sym_a.symbol_name}", f"{sym_b.file_path}:{sym_b.symbol_name}"]))
                if pair_key in reported_clones:
                    continue
                sim = jaccard_similarity(fingerprints[i], fingerprints[j])
                if sim >= thresh:
                    reported_clones.add(pair_key)
                    is_act, conf, rec = classifier.classify_finding(
                        "redundancy", "code_clone", sym_a.file_path, sym_a.qualified_name, sym_a.source_code
                    )
                    audit_rep.redundancies.append(
                        Finding(
                            "redundancy",
                            "code_clone",
                            f"Duplicate clone: `{sym_a.qualified_name}` ~ `{sym_b.qualified_name}` ({int(sim*100)}% match)",
                            sym_a.file_path,
                            sym_a.start_line,
                            conf,
                            is_act,
                            rec,
                            related_file=sym_b.file_path,
                            related_line=sym_b.start_line,
                        )
                    )

        combined_source = "\n".join(file_sources.values()) + "\n" + test_corpus
        all_exports: set[str] = set()
        for ftext in file_sources.values():
            for m in re.finditer(r"__all__\s*=\s*\[([^\]]+)\]", ftext):
                for name in re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)):
                    all_exports.add(name)

        for sym in all_symbols:
            if sym.symbol_name.startswith("_") or sym.symbol_name in ("main", "app", "cli", "run", "init"):
                continue
            if sym.symbol_name in all_exports:
                continue

            # Skip framework-dispatched entry points (e.g. @app.command(), @router, etc.)
            source_lines = file_sources.get(sym.file_path, "").splitlines()
            idx = sym.start_line - 2
            is_entrypoint = False
            while idx >= 0:
                line_str = source_lines[idx].strip()
                if line_str.startswith("@"):
                    if any(fw in line_str for fw in ("app.", "router.", "route", "command", "click.", "pytest.")):
                        is_entrypoint = True
                        break
                    idx -= 1
                elif not line_str:
                    idx -= 1
                else:
                    break
            if is_entrypoint:
                continue

            occurrences = len(re.findall(rf"\b{re.escape(sym.symbol_name)}\b", combined_source))
            if occurrences <= 1:
                is_act, conf, rec = classifier.classify_finding(
                    "redundancy", "dead_code", sym.file_path, sym.qualified_name, sym.source_code
                )
                audit_rep.redundancies.append(
                    Finding("redundancy", "dead_code", f"Unused dead symbol: `{sym.qualified_name}` (0 callers)", sym.file_path, sym.start_line, conf, is_act, rec)
                )

        return audit_rep

    def _discover_source_files(self) -> list[Path]:
        """Find all non-test source files inside configured source directories."""
        found: list[Path] = []
        seen: set[Path] = set()
        allowed_exts = self._allowed_extensions()

        for src_dir_name in self.config.source_dirs:
            base = (self.repo_root / src_dir_name).resolve()
            if not base.exists():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in allowed_exts:
                    continue
                resolved = path.resolve()
                if resolved in seen:
                    continue
                rel_parts = resolved.relative_to(self.repo_root).parts
                if any(part in self.config.exclude_dirs for part in rel_parts):
                    continue
                adapter = get_adapter_for_path(resolved)
                if adapter and adapter.is_test_file(resolved, rel_parts, self.config.tests_dir):
                    continue
                seen.add(resolved)
                found.append(resolved)
        return found

    def _load_test_corpus(self) -> str:
        """Read all test files across the repository into a combined lookup string."""
        chunks: list[str] = []
        seen: set[Path] = set()
        allowed_exts = self._allowed_extensions()

        for path in sorted(self.repo_root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed_exts:
                continue
            resolved = path.resolve()
            rel_parts = resolved.relative_to(self.repo_root).parts
            if any(part in self.config.exclude_dirs for part in rel_parts):
                continue
            adapter = get_adapter_for_path(resolved)
            if adapter and adapter.is_test_file(resolved, rel_parts, self.config.tests_dir):
                if resolved not in seen:
                    seen.add(resolved)
                    try:
                        chunks.append(resolved.read_text(encoding="utf-8", errors="ignore"))
                    except OSError:
                        pass
        return "\n".join(chunks)

    def _load_coverage_map(self) -> dict[str, set[int]]:
        """Load line execution data from coverage.json, lcov.info, or cover.out if available."""
        result: dict[str, set[int]] = {}

        # 1. Python coverage.json
        cov_file = self.repo_root / "coverage.json"
        if cov_file.exists():
            try:
                data = json.loads(cov_file.read_text(encoding="utf-8"))
                for fpath, info in data.get("files", {}).items():
                    norm = Path(fpath).as_posix()
                    result[norm] = set(info.get("executed_lines", []))
            except (json.JSONDecodeError, OSError, KeyError):
                pass

        # 2. LCOV (TypeScript/JS Vitest/Jest, Rust cargo-llvm-cov, etc.)
        for lcov_rel in ("lcov.info", "coverage/lcov.info"):
            lcov_file = self.repo_root / lcov_rel
            if lcov_file.exists():
                try:
                    current_file: str | None = None
                    for line in lcov_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.startswith("SF:"):
                            raw_sf = Path(line[3:].strip())
                            if raw_sf.is_absolute() and raw_sf.is_relative_to(self.repo_root):
                                current_file = raw_sf.relative_to(self.repo_root).as_posix()
                            else:
                                current_file = raw_sf.as_posix()
                            result.setdefault(current_file, set())
                        elif line.startswith("DA:") and current_file:
                            parts = line[3:].split(",")
                            if len(parts) >= 2 and int(parts[1]) > 0:
                                result[current_file].add(int(parts[0]))
                except (ValueError, OSError):
                    pass

        # 3. Go coverprofile (cover.out / coverage.out)
        for go_cov_rel in ("cover.out", "coverage.out"):
            go_cov_file = self.repo_root / go_cov_rel
            if go_cov_file.exists():
                try:
                    for line in go_cov_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if not line or line.startswith("mode:"):
                            continue
                        # Format: module/path/file.go:startLine.col,endLine.col numStmt count
                        loc_part, *rest = line.split()
                        if len(rest) >= 2 and int(rest[-1]) > 0 and ":" in loc_part:
                            file_part, span_part = loc_part.rsplit(":", 1)
                            start_s, end_s = span_part.split(",")
                            start_ln = int(start_s.split(".")[0])
                            end_ln = int(end_s.split(".")[0])
                            for suffix_len in range(1, len(Path(file_part).parts) + 1):
                                candidate = "/".join(Path(file_part).parts[-suffix_len:])
                                if (self.repo_root / candidate).exists():
                                    result.setdefault(candidate, set()).update(
                                        range(start_ln, end_ln + 1)
                                    )
                                    break
                except (ValueError, OSError):
                    pass

        return result

    def _parse_file(
        self,
        file_path: Path,
        rel_path: str,
        test_corpus: str,
        coverage_map: dict[str, set[int]],
    ) -> list[CodeSymbol]:
        """Parse a single source file using its corresponding LanguageAdapter."""
        adapter: LanguageAdapter | None = get_adapter_for_path(file_path)
        if adapter is None:
            return []
        try:
            source = file_path.read_text(encoding="utf-8")
        except Exception:
            return []
        return adapter.parse_symbols(
            source=source,
            rel_path=rel_path,
            test_corpus=test_corpus,
            coverage_map=coverage_map,
            include_private=self.config.include_private_symbols,
        )
