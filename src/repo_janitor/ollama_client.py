"""Ollama client wrapper for generating docstrings, README updates, and pytest suites."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Protocol

from repo_janitor.config import JanitorConfig
from repo_janitor.scanner import CodeSymbol


class LLMGenerator(Protocol):
    """Protocol for local LLM generation (enables easy mocking in tests)."""

    def generate_docstring(self, symbol: CodeSymbol) -> str:
        """Return a concise PEP 257 docstring text (without outer triple quotes)."""
        ...

    def generate_unit_test(
        self,
        symbol: CodeSymbol,
        module_import_path: str,
        full_file_source: str,
        previous_error: str | None = None,
    ) -> str:
        """Return complete pytest code testing the given symbol."""
        ...

    def generate_readme_section(
        self, existing_readme: str, missing_modules: list[str], module_summaries: dict[str, str]
    ) -> str:
        """Return updated README markdown documenting missing modules."""
        ...


class OllamaClient:
    """Interacts with a local Ollama instance via SDK or HTTP API."""

    def __init__(self, config: JanitorConfig) -> None:
        self.config = config

    def is_available(self) -> bool:
        """Check if the local Ollama server is reachable."""
        url = f"{self.config.ollama_host.rstrip('/')}/api/tags"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _chat(self, system_prompt: str, user_prompt: str) -> str:
        """Send a chat completion request to Ollama."""
        try:
            import ollama  # type: ignore

            client = ollama.Client(host=self.config.ollama_host)
            response = client.chat(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                options={"temperature": self.config.temperature},
            )
            if isinstance(response, dict):
                return response.get("message", {}).get("content", "")
            return getattr(response.message, "content", "")
        except Exception as exc:
            # Fallback to direct HTTP request if SDK call or import fails
            return self._http_chat(system_prompt, user_prompt, prev_exc=exc)

    def _http_chat(
        self, system_prompt: str, user_prompt: str, prev_exc: Exception | None = None
    ) -> str:
        url = f"{self.config.ollama_host.rstrip('/')}/api/chat"
        payload = json.dumps(
            {
                "model": self.config.model,
                "stream": False,
                "options": {"temperature": self.config.temperature},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("message", {}).get("content", "")
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.config.ollama_host} "
                f"(model={self.config.model}): {prev_exc or exc}"
            ) from exc

    def generate_docstring(self, symbol: CodeSymbol) -> str:
        """Generate a clean documentation comment/docstring for a function, method, or class."""
        from repo_janitor.languages import get_language_adapter

        adapter = get_language_adapter(symbol.language)
        system_prompt = (
            f"You are an expert {adapter.display_name} documentation assistant. "
            f"Write a clear, concise {adapter.doc_style} for the provided {adapter.display_name} symbol. "
            "Return ONLY the raw documentation text itself (do not wrap in code fences, "
            "do not repeat the declaration, and do not include outer comment delimiters or triple quotes)."
        )
        user_prompt = (
            f"File: {symbol.file_path}\n"
            f"Symbol ({symbol.kind}): {symbol.qualified_name}\n\n"
            f"Source code:\n```{adapter.code_fence}\n{symbol.source_code}\n```\n\n"
            "Write the documentation content:"
        )
        raw = self._chat(system_prompt, user_prompt).strip()
        cleaned = self._strip_code_fences(raw).strip()
        cleaned = cleaned.strip('"""').strip("'''").strip()
        return cleaned or f"{symbol.qualified_name} implementation."

    def generate_unit_test(
        self,
        symbol: CodeSymbol,
        module_import_path: str,
        full_file_source: str,
        previous_error: str | None = None,
    ) -> str:
        """Generate a self-contained unit test module for an uncovered function/method."""
        from repo_janitor.languages import get_language_adapter

        adapter = get_language_adapter(symbol.language)
        system_prompt = (
            f"You are an expert {adapter.display_name} test engineer. "
            f"Write a self-contained {adapter.test_framework} test file for the target symbol. "
            f"Output ONLY valid {adapter.display_name} code inside a ```{adapter.code_fence} ... ``` block. "
            "Do not use external network calls; mock I/O if needed."
        )
        retry_context = ""
        if previous_error:
            retry_context = (
                f"\nYour previous test attempt failed with this test runner error:\n"
                f"```\n{previous_error}\n```\nFix the test so that `{adapter.default_test_command}` passes.\n"
            )

        user_prompt = (
            f"Target file: {symbol.file_path}\n"
            f"Import / package target: `{module_import_path}`\n"
            f"Target symbol ({symbol.kind}): `{symbol.qualified_name}`\n\n"
            f"Full module source:\n```{adapter.code_fence}\n{full_file_source}\n```\n"
            f"{retry_context}\n"
            f"Write a {adapter.test_framework} file that imports/uses `{module_import_path}` and thoroughly "
            f"tests `{symbol.qualified_name}`."
        )
        raw = self._chat(system_prompt, user_prompt)
        return self._strip_code_fences(raw)

    def generate_readme_section(
        self,
        existing_readme: str,
        missing_modules: list[str],
        module_summaries: dict[str, str],
    ) -> str:
        """Generate or update README.md with documentation for undocumented modules."""
        system_prompt = (
            "You are a technical writer maintaining a software repository README.md. "
            "Return the complete updated Markdown for README.md."
        )
        summary_lines = "\n".join(
            f"- `{mod}`: {desc}" for mod, desc in module_summaries.items()
        )
        user_prompt = (
            f"Existing README.md:\n```markdown\n{existing_readme}\n```\n\n"
            f"Undocumented modules to include: {', '.join(missing_modules)}\n"
            f"Module details:\n{summary_lines}\n\n"
            "Return the updated README.md content:"
        )
        raw = self._chat(system_prompt, user_prompt)
        return self._strip_code_fences(raw)

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Extract content inside markdown code fences if present."""
        match = re.search(
            r"```(?:python|typescript|ts|javascript|js|go|golang|rust|rs|markdown|md)?\s*\n(.*?)```",
            text,
            re.DOTALL,
        )
        if match:
            return match.group(1).strip()
        return text.strip()
