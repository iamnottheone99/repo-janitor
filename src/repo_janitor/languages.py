"""Language adapters for multi-language symbol parsing, doc-comment injection, and test conventions."""

from __future__ import annotations

import ast
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from repo_janitor.scanner import CodeSymbol


class LanguageAdapter(ABC):
    """Base interface for language-specific scanning, doc injection, and test generation."""

    name: str
    display_name: str
    code_fence: str
    file_extensions: tuple[str, ...]
    doc_style: str
    test_framework: str
    default_test_command: str

    @abstractmethod
    def is_test_file(self, path: Path, rel_parts: tuple[str, ...], tests_dir: str) -> bool:
        """Return True if the file is a test module rather than production source."""

    @abstractmethod
    def parse_symbols(
        self,
        source: str,
        rel_path: str,
        test_corpus: str,
        coverage_map: dict[str, set[int]],
        include_private: bool = False,
    ) -> list[CodeSymbol]:
        """Extract functions, methods, and classes/structs from source text."""

    @abstractmethod
    def inject_docstring(
        self, source: str, symbol_name: str, qualified_name: str, docstring: str
    ) -> str:
        """Insert a formatted doc comment for the target symbol and validate syntax."""

    @abstractmethod
    def derive_import_target(self, rel_path: str) -> str:
        """Compute the module import path or package name for generated unit tests."""

    @abstractmethod
    def generated_test_rel_path(self, sym: CodeSymbol, slug: str, tests_dir: str) -> str:
        """Return the relative file path where a generated unit test should be written."""


def _is_symbol_tested(
    symbol_name: str,
    rel_path: str,
    body_start_line: int,
    end_line: int,
    test_corpus: str,
    coverage_map: dict[str, set[int]],
) -> bool:
    """Check line-level coverage map first, falling back to test corpus symbol lookup."""
    if coverage_map:
        executed = coverage_map.get(rel_path, set())
        body_lines = set(range(body_start_line, end_line + 1))
        return bool(executed & body_lines)
    return (
        f"test_{symbol_name}" in test_corpus
        or f"Test{symbol_name}" in test_corpus
        or f"{symbol_name}(" in test_corpus
        or f".{symbol_name}(" in test_corpus
    )


def _find_matching_brace_end(lines: list[str], start_idx: int) -> int:
    """Find the 1-indexed closing line for a brace-delimited block starting at start_idx."""
    depth = 0
    opened = False
    for idx in range(start_idx, len(lines)):
        line = re.sub(r"//.*$", "", lines[idx])
        for ch in line:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return idx + 1
    return min(len(lines), start_idx + 1)


class PythonAdapter(LanguageAdapter):
    """Language adapter for Python repositories (AST + PEP 257 + pytest)."""

    name = "python"
    display_name = "Python"
    code_fence = "python"
    file_extensions = (".py",)
    doc_style = "PEP 257 docstring"
    test_framework = "pytest"
    default_test_command = "pytest -q"

    def is_test_file(self, path: Path, rel_parts: tuple[str, ...], tests_dir: str) -> bool:
        return (
            tests_dir in rel_parts
            or path.name.startswith("test_")
            or path.name.endswith("_test.py")
            or path.name == "conftest.py"
        )

    def parse_symbols(
        self,
        source: str,
        rel_path: str,
        test_corpus: str,
        coverage_map: dict[str, set[int]],
        include_private: bool = False,
    ) -> list[CodeSymbol]:
        from repo_janitor.scanner import CodeSymbol

        try:
            tree = ast.parse(source, filename=rel_path)
        except Exception:
            return []

        lines = source.splitlines()
        symbols: list[CodeSymbol] = []

        def build(
            node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
            kind: str,
            qualified_name: str,
        ) -> CodeSymbol | None:
            if not include_private and node.name.startswith("_") and node.name != "__init__":
                return None
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", start_line) or start_line
            body_start_line = node.body[0].lineno if node.body else start_line + 1
            has_doc = ast.get_docstring(node) is not None
            snippet = "\n".join(lines[start_line - 1 : end_line])
            sig_line = lines[start_line - 1].strip()
            is_tested = _is_symbol_tested(
                node.name, rel_path, body_start_line, end_line, test_corpus, coverage_map
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
                language=self.name,
            )

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sym = build(node, "function", node.name)
                if sym:
                    symbols.append(sym)
            elif isinstance(node, ast.ClassDef):
                cls_sym = build(node, "class", node.name)
                if cls_sym:
                    symbols.append(cls_sym)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_sym = build(item, "method", f"{node.name}.{item.name}")
                        if method_sym:
                            symbols.append(method_sym)

        return symbols

    def inject_docstring(
        self, source: str, symbol_name: str, qualified_name: str, docstring: str
    ) -> str:
        tree = ast.parse(source)
        target_node: ast.AST | None = None

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == qualified_name:
                    target_node = node
                    break
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if f"{node.name}.{child.name}" == qualified_name:
                                target_node = child
                                break

        if target_node is None or not getattr(target_node, "body", None):
            raise ValueError(f"Symbol {qualified_name} not found in AST")

        if ast.get_docstring(target_node) is not None:
            return source

        lines = source.splitlines(keepends=True)
        first_stmt = target_node.body[0]  # type: ignore[attr-defined]
        insert_idx = first_stmt.lineno - 1

        stmt_line = lines[insert_idx]
        indent_len = len(stmt_line) - len(stmt_line.lstrip(" \t"))
        indent = stmt_line[:indent_len] or "    "

        clean_doc = docstring.strip().replace('"""', "'''")
        doc_lines = clean_doc.splitlines()
        if len(doc_lines) == 1:
            formatted_doc = f'{indent}"""{doc_lines[0]}"""\n'
        else:
            inner = "\n".join(f"{indent}{line}" if line.strip() else "" for line in doc_lines)
            formatted_doc = f'{indent}"""\n{inner}\n{indent}"""\n'

        lines.insert(insert_idx, formatted_doc)
        updated = "".join(lines)
        ast.parse(updated)
        return updated

    def derive_import_target(self, rel_path: str) -> str:
        parts = list(Path(rel_path).with_suffix("").parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def generated_test_rel_path(self, sym: CodeSymbol, slug: str, tests_dir: str) -> str:
        return f"{tests_dir}/test_janitor_{slug.replace('-', '_')}.py"


class TypeScriptJSAdapter(LanguageAdapter):
    """Language adapter for TypeScript and JavaScript repositories (JSDoc/TSDoc + Vitest/Jest)."""

    name = "typescript"
    display_name = "TypeScript/JavaScript"
    code_fence = "typescript"
    file_extensions = (".ts", ".tsx", ".js", ".jsx")
    doc_style = "JSDoc / TSDoc comment (/** ... */)"
    test_framework = "vitest / jest"
    default_test_command = "npm test -- --run"

    _DECL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
        (
            "function",
            re.compile(
                r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[<(]"
            ),
        ),
        (
            "function",
            re.compile(
                r"^\s*(?:export\s+)?const\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*=>"
            ),
        ),
        (
            "class",
            re.compile(
                r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
            ),
        ),
    ]

    def is_test_file(self, path: Path, rel_parts: tuple[str, ...], tests_dir: str) -> bool:
        name = path.name
        return (
            tests_dir in rel_parts
            or "__tests__" in rel_parts
            or name.endswith(".d.ts")
            or ".test." in name
            or ".spec." in name
        )

    @staticmethod
    def _has_preceding_jsdoc(lines: list[str], decl_idx: int) -> bool:
        idx = decl_idx - 1
        while idx >= 0 and (
            not lines[idx].strip() or lines[idx].strip().startswith("@")
        ):
            idx -= 1
        if idx < 0:
            return False
        stripped = lines[idx].strip()
        return stripped.endswith("*/") or stripped.startswith("//")

    def parse_symbols(
        self,
        source: str,
        rel_path: str,
        test_corpus: str,
        coverage_map: dict[str, set[int]],
        include_private: bool = False,
    ) -> list[CodeSymbol]:
        from repo_janitor.scanner import CodeSymbol

        lines = source.splitlines()
        symbols: list[CodeSymbol] = []

        for idx, line in enumerate(lines):
            for kind, pattern in self._DECL_PATTERNS:
                match = pattern.match(line)
                if not match:
                    continue
                name = match.group(1)
                if not include_private and name.startswith("_"):
                    break
                start_line = idx + 1
                end_line = _find_matching_brace_end(lines, idx)
                body_start = min(end_line, start_line + 1)
                has_doc = self._has_preceding_jsdoc(lines, idx)
                snippet = "\n".join(lines[idx:end_line])
                is_tested = _is_symbol_tested(
                    name, rel_path, body_start, end_line, test_corpus, coverage_map
                )
                symbols.append(
                    CodeSymbol(
                        file_path=rel_path,
                        symbol_name=name,
                        qualified_name=name,
                        kind=kind,
                        start_line=start_line,
                        end_line=end_line,
                        body_start_line=body_start,
                        signature=line.strip(),
                        has_docstring=has_doc,
                        is_tested=is_tested,
                        source_code=snippet,
                        language=self.name,
                    )
                )
                break

        return symbols

    def inject_docstring(
        self, source: str, symbol_name: str, qualified_name: str, docstring: str
    ) -> str:
        lines = source.splitlines(keepends=True)
        raw_lines = source.splitlines()
        target_idx: int | None = None

        for idx, line in enumerate(raw_lines):
            for _, pattern in self._DECL_PATTERNS:
                match = pattern.match(line)
                if match and match.group(1) in (symbol_name, qualified_name):
                    target_idx = idx
                    break
            if target_idx is not None:
                break

        if target_idx is None:
            raise ValueError(f"Symbol {qualified_name} not found in {self.display_name} file")

        if self._has_preceding_jsdoc(raw_lines, target_idx):
            return source

        decl_line = lines[target_idx]
        indent = decl_line[: len(decl_line) - len(decl_line.lstrip(" \t"))]
        clean_doc = docstring.strip()
        clean_doc = re.sub(r"^/\*\*?\s*|\s*\*/$", "", clean_doc).strip()
        doc_lines = [
            re.sub(r"^\s*\*\s?", "", dl).strip() for dl in clean_doc.splitlines() if dl.strip()
        ]
        if len(doc_lines) <= 1:
            text = doc_lines[0] if doc_lines else f"{qualified_name} implementation."
            comment = f"{indent}/** {text} */\n"
        else:
            body = "\n".join(f"{indent} * {dl}" for dl in doc_lines)
            comment = f"{indent}/**\n{body}\n{indent} */\n"

        lines.insert(target_idx, comment)
        return "".join(lines)

    def derive_import_target(self, rel_path: str) -> str:
        no_ext = Path(rel_path).with_suffix("").as_posix()
        return f"../{no_ext}"

    def generated_test_rel_path(self, sym: CodeSymbol, slug: str, tests_dir: str) -> str:
        ext = ".ts" if sym.file_path.endswith((".ts", ".tsx")) else ".js"
        return f"{tests_dir}/{slug}.janitor.test{ext}"


class GoAdapter(LanguageAdapter):
    """Language adapter for Go repositories (GoDoc comments + go test)."""

    name = "go"
    display_name = "Go"
    code_fence = "go"
    file_extensions = (".go",)
    doc_style = "GoDoc comment (// SymbolName ...)"
    test_framework = "go test (testing package)"
    default_test_command = "go test ./..."

    _FUNC_RE = re.compile(
        r"^\s*func\s+(?:\(\s*\w+\s+\*?([A-Za-z_][A-Za-z0-9_]*)\s*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\("
    )
    _TYPE_RE = re.compile(r"^\s*type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b")

    def is_test_file(self, path: Path, rel_parts: tuple[str, ...], tests_dir: str) -> bool:
        return path.name.endswith("_test.go")

    @staticmethod
    def _has_preceding_godoc(lines: list[str], decl_idx: int) -> bool:
        return decl_idx > 0 and lines[decl_idx - 1].strip().startswith("//")

    def parse_symbols(
        self,
        source: str,
        rel_path: str,
        test_corpus: str,
        coverage_map: dict[str, set[int]],
        include_private: bool = False,
    ) -> list[CodeSymbol]:
        from repo_janitor.scanner import CodeSymbol

        lines = source.splitlines()
        symbols: list[CodeSymbol] = []

        for idx, line in enumerate(lines):
            func_match = self._FUNC_RE.match(line)
            type_match = self._TYPE_RE.match(line) if not func_match else None
            if not func_match and not type_match:
                continue

            if func_match:
                receiver, name = func_match.group(1), func_match.group(2)
                kind = "method" if receiver else "function"
                qualified = f"{receiver}.{name}" if receiver else name
            else:
                assert type_match is not None
                name = type_match.group(1)
                kind = "class"
                qualified = name

            if not include_private and name[0].islower() and name != "main":
                continue

            start_line = idx + 1
            end_line = _find_matching_brace_end(lines, idx)
            body_start = min(end_line, start_line + 1)
            has_doc = self._has_preceding_godoc(lines, idx)
            snippet = "\n".join(lines[idx:end_line])
            is_tested = _is_symbol_tested(
                name, rel_path, body_start, end_line, test_corpus, coverage_map
            )
            symbols.append(
                CodeSymbol(
                    file_path=rel_path,
                    symbol_name=name,
                    qualified_name=qualified,
                    kind=kind,
                    start_line=start_line,
                    end_line=end_line,
                    body_start_line=body_start,
                    signature=line.strip(),
                    has_docstring=has_doc,
                    is_tested=is_tested,
                    source_code=snippet,
                    language=self.name,
                )
            )

        return symbols

    def inject_docstring(
        self, source: str, symbol_name: str, qualified_name: str, docstring: str
    ) -> str:
        lines = source.splitlines(keepends=True)
        raw_lines = source.splitlines()
        target_idx: int | None = None

        for idx, line in enumerate(raw_lines):
            func_match = self._FUNC_RE.match(line)
            if func_match:
                receiver, name = func_match.group(1), func_match.group(2)
                qname = f"{receiver}.{name}" if receiver else name
                if qname == qualified_name or name == symbol_name:
                    target_idx = idx
                    break
            type_match = self._TYPE_RE.match(line)
            if type_match and type_match.group(1) in (symbol_name, qualified_name):
                target_idx = idx
                break

        if target_idx is None:
            raise ValueError(f"Symbol {qualified_name} not found in Go source")

        if self._has_preceding_godoc(raw_lines, target_idx):
            return source

        decl_line = lines[target_idx]
        indent = decl_line[: len(decl_line) - len(decl_line.lstrip(" \t"))]
        doc_lines = [
            re.sub(r"^\s*//\s?", "", dl).strip()
            for dl in docstring.strip().splitlines()
            if dl.strip()
        ]
        if not doc_lines:
            doc_lines = [f"{symbol_name} implementation."]
        comment = "".join(f"{indent}// {dl}\n" for dl in doc_lines)
        lines.insert(target_idx, comment)
        return "".join(lines)

    def derive_import_target(self, rel_path: str) -> str:
        parent = Path(rel_path).parent.as_posix()
        return parent if parent != "." else "main"

    def generated_test_rel_path(self, sym: CodeSymbol, slug: str, tests_dir: str) -> str:
        parent = Path(sym.file_path).parent.as_posix()
        safe_slug = slug.replace("-", "_")
        if parent and parent != ".":
            return f"{parent}/{safe_slug}_janitor_test.go"
        return f"{safe_slug}_janitor_test.go"


class RustAdapter(LanguageAdapter):
    """Language adapter for Rust repositories (Rustdoc /// comments + cargo test)."""

    name = "rust"
    display_name = "Rust"
    code_fence = "rust"
    file_extensions = (".rs",)
    doc_style = "Rustdoc comment (/// ...)"
    test_framework = "cargo test"
    default_test_command = "cargo test --quiet"

    _FN_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    _STRUCT_RE = re.compile(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    )

    def is_test_file(self, path: Path, rel_parts: tuple[str, ...], tests_dir: str) -> bool:
        return tests_dir in rel_parts or path.name.endswith("_test.rs")

    @staticmethod
    def _has_preceding_rustdoc(lines: list[str], decl_idx: int) -> bool:
        idx = decl_idx - 1
        while idx >= 0 and lines[idx].strip().startswith("#["):
            idx -= 1
        return idx >= 0 and (
            lines[idx].strip().startswith("///") or lines[idx].strip().endswith("*/")
        )

    def parse_symbols(
        self,
        source: str,
        rel_path: str,
        test_corpus: str,
        coverage_map: dict[str, set[int]],
        include_private: bool = False,
    ) -> list[CodeSymbol]:
        from repo_janitor.scanner import CodeSymbol

        lines = source.splitlines()
        symbols: list[CodeSymbol] = []

        for idx, line in enumerate(lines):
            fn_match = self._FN_RE.match(line)
            st_match = self._STRUCT_RE.match(line) if not fn_match else None
            if not fn_match and not st_match:
                continue

            is_pub = line.lstrip().startswith("pub")
            name = fn_match.group(1) if fn_match else st_match.group(1)  # type: ignore[union-attr]
            if name == "main" or (not include_private and not is_pub and name.startswith("_")):
                continue

            kind = "function" if fn_match else "class"
            start_line = idx + 1
            end_line = _find_matching_brace_end(lines, idx)
            body_start = min(end_line, start_line + 1)
            has_doc = self._has_preceding_rustdoc(lines, idx)
            snippet = "\n".join(lines[idx:end_line])
            is_tested = _is_symbol_tested(
                name, rel_path, body_start, end_line, test_corpus, coverage_map
            )
            symbols.append(
                CodeSymbol(
                    file_path=rel_path,
                    symbol_name=name,
                    qualified_name=name,
                    kind=kind,
                    start_line=start_line,
                    end_line=end_line,
                    body_start_line=body_start,
                    signature=line.strip(),
                    has_docstring=has_doc,
                    is_tested=is_tested,
                    source_code=snippet,
                    language=self.name,
                )
            )

        return symbols

    def inject_docstring(
        self, source: str, symbol_name: str, qualified_name: str, docstring: str
    ) -> str:
        lines = source.splitlines(keepends=True)
        raw_lines = source.splitlines()
        target_idx: int | None = None

        for idx, line in enumerate(raw_lines):
            fn_match = self._FN_RE.match(line)
            st_match = self._STRUCT_RE.match(line)
            matched_name = (
                fn_match.group(1)
                if fn_match
                else (st_match.group(1) if st_match else None)
            )
            if matched_name in (symbol_name, qualified_name):
                target_idx = idx
                break

        if target_idx is None:
            raise ValueError(f"Symbol {qualified_name} not found in Rust source")

        if self._has_preceding_rustdoc(raw_lines, target_idx):
            return source

        # Insert above any #[...] attributes immediately preceding the declaration
        insert_idx = target_idx
        while insert_idx > 0 and raw_lines[insert_idx - 1].strip().startswith("#["):
            insert_idx -= 1

        decl_line = lines[insert_idx]
        indent = decl_line[: len(decl_line) - len(decl_line.lstrip(" \t"))]
        doc_lines = [
            re.sub(r"^\s*///\s?", "", dl).strip()
            for dl in docstring.strip().splitlines()
            if dl.strip()
        ]
        if not doc_lines:
            doc_lines = [f"{symbol_name} implementation."]
        comment = "".join(f"{indent}/// {dl}\n" for dl in doc_lines)
        lines.insert(insert_idx, comment)
        return "".join(lines)

    def derive_import_target(self, rel_path: str) -> str:
        stem = Path(rel_path).stem
        return "crate" if stem in ("lib", "main") else f"crate::{stem}"

    def generated_test_rel_path(self, sym: CodeSymbol, slug: str, tests_dir: str) -> str:
        return f"{tests_dir}/test_janitor_{slug.replace('-', '_')}.rs"


ADAPTERS: dict[str, LanguageAdapter] = {
    "python": PythonAdapter(),
    "typescript": TypeScriptJSAdapter(),
    "javascript": TypeScriptJSAdapter(),
    "go": GoAdapter(),
    "rust": RustAdapter(),
}

EXTENSION_TO_ADAPTER: dict[str, LanguageAdapter] = {
    ext: adapter
    for name, adapter in ADAPTERS.items()
    if name != "javascript"
    for ext in adapter.file_extensions
}


def get_language_adapter(language: str) -> LanguageAdapter:
    """Return the LanguageAdapter for a given language name (defaults to Python)."""
    return ADAPTERS.get(language.lower(), ADAPTERS["python"])


def get_adapter_for_path(path: str | Path) -> LanguageAdapter | None:
    """Return the LanguageAdapter matching a file's extension, or None if unsupported."""
    suffix = Path(path).suffix.lower()
    return EXTENSION_TO_ADAPTER.get(suffix)


def detect_repo_language(repo_root: Path) -> str:
    """Auto-detect primary repository language from project manifest files."""
    if (repo_root / "pyproject.toml").exists() or (repo_root / "setup.py").exists():
        return "python"
    if (repo_root / "tsconfig.json").exists():
        return "typescript"
    if (repo_root / "package.json").exists():
        return "typescript"
    if (repo_root / "go.mod").exists():
        return "go"
    if (repo_root / "Cargo.toml").exists():
        return "rust"
    return "python"
