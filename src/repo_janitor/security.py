"""Security guardrails, AST validation, and input sanitization for repo-janitor."""

from __future__ import annotations

import ast
import re

PROHIBITED_PYTHON_IMPORTS = {
    "os",
    "subprocess",
    "pty",
    "shutil",
    "socket",
    "commands",
    "multiprocessing",
    "ctypes",
    "urllib.request",
    "http.client",
    "requests",
    "httpx",
    "aiohttp",
}

PROHIBITED_PYTHON_CALLS = {
    "eval",
    "exec",
    "__import__",
    "compile",
}

DANGEROUS_SHELL_INJECTION = re.compile(r"[;&|`$><\n\r]")


class SecurityValidationError(ValueError):
    """Raised when generated code or configuration fails safety validation."""


def validate_generated_code_safety(source: str, language: str = "python") -> None:
    """Inspect LLM-generated code for malicious calls or exfiltration imports before execution."""
    if language.lower() == "python":
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_pkg = alias.name.split(".")[0]
                        if top_pkg in PROHIBITED_PYTHON_IMPORTS:
                            raise SecurityValidationError(
                                f"Prohibited module imported in generated test: '{alias.name}'"
                            )
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    top_pkg = mod.split(".")[0]
                    if top_pkg in PROHIBITED_PYTHON_IMPORTS:
                        raise SecurityValidationError(
                            f"Prohibited module imported in generated test: '{mod}'"
                        )
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id in PROHIBITED_PYTHON_CALLS:
                        raise SecurityValidationError(
                            f"Prohibited function called in generated test: '{func.id}()'"
                        )
        except SyntaxError:
            # Syntax errors will naturally be caught by the test runner
            pass
    else:
        dangerous_patterns = [
            (r"\bchild_process\b", "child_process execution"),
            (r"\bos/exec\b", "Go os/exec execution"),
            (r"\bstd::process::Command\b", "Rust Command execution"),
            (r"\beval\s*\(", "eval() execution"),
        ]
        for pattern, desc in dangerous_patterns:
            if re.search(pattern, source):
                raise SecurityValidationError(f"Prohibited pattern in generated test: {desc}")


def validate_test_command_safety(command: str) -> None:
    """Validate that repository-configured test command does not perform arbitrary shell chaining."""
    # Strip quoted string arguments before inspecting for shell control operators
    unquoted = re.sub(r'"[^"]*"|\'[^\']*\'', "", command)
    if DANGEROUS_SHELL_INJECTION.search(unquoted):
        raise SecurityValidationError(
            f"Prohibited shell metacharacters detected in test_command: '{command}'. "
            "Shell chaining (;&|`$><) is blocked for safety."
        )
