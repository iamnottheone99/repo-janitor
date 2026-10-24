"""Configuration models and loader for Repo Janitor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class JanitorConfig:
    """Configuration for autonomous repository maintenance runs."""

    model: str = "qwen2.5-coder:7b"
    provider: str = "ollama"  # "ollama" | "openai" | "vllm" | "lmstudio"
    api_base: str = ""        # Custom base URL (e.g. http://localhost:1234/v1, http://localhost:8000/v1)
    api_key: str = ""         # Optional API key for OpenAI-compliant endpoints
    ollama_host: str = "http://localhost:11434"
    temperature: float = 0.1
    max_retries: int = 2
    branch_prefix: str = "janitor"
    worktree_dir: str = ".janitor/worktrees"
    test_command: str = "pytest -q"
    source_dirs: list[str] = field(default_factory=lambda: ["src", "."])
    tests_dir: str = "tests"
    exclude_dirs: list[str] = field(
        default_factory=lambda: [
            ".git",
            ".janitor",
            ".venv",
            "venv",
            "__pycache__",
            "node_modules",
            "dist",
            "build",
            ".pytest_cache",
        ]
    )
    include_private_symbols: bool = False
    max_tasks_per_run: int = 5
    language: str = "auto"
    similarity_threshold: float = 0.85
    min_clone_lines: int = 3
    classifier_model: str = "fast"

    def effective_api_base(self) -> str:
        """Resolve the base endpoint URL for the configured LLM provider."""
        if self.api_base:
            return self.api_base.rstrip("/")
        prov = self.provider.lower()
        if prov == "vllm":
            return "http://localhost:8000/v1"
        if prov == "lmstudio":
            return "http://localhost:1234/v1"
        if prov in ("openai", "openrouter"):
            return "https://api.openai.com/v1"
        return self.ollama_host.rstrip("/")

    def effective_api_key(self) -> str:
        """Resolve the API key for OpenAI-compliant endpoints safely without leaking to arbitrary hosts."""
        import os
        from urllib.parse import urlparse

        if self.api_key:
            return self.api_key

        base = self.effective_api_base()
        hostname = (urlparse(base).hostname or "").lower()

        # Local servers (Ollama, vLLM, LM Studio) do not require ambient OpenAI keys
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return ""

        # Only forward ambient OPENAI_API_KEY to verified OpenAI/OpenRouter domains or openai provider
        if self.provider.lower() in ("openai", "openrouter") or hostname.endswith(("openai.com", "openrouter.ai")):
            return os.environ.get("OPENAI_API_KEY", "")

        return ""

    def effective_test_command(self, repo_root: Path) -> str:
        """Resolve the test runner command for the repository."""
        from repo_janitor.languages import detect_repo_language, get_language_adapter

        if self.test_command and self.test_command != "auto":
            # If default 'pytest -q' is still set on a non-Python repo, adapt automatically
            if self.test_command == "pytest -q":
                lang = (
                    self.language
                    if self.language != "auto"
                    else detect_repo_language(repo_root)
                )
                if lang != "python":
                    return get_language_adapter(lang).default_test_command
            return self.test_command

        lang = (
            self.language
            if self.language != "auto"
            else detect_repo_language(repo_root)
        )
        return get_language_adapter(lang).default_test_command

    @classmethod
    def load(cls, repo_root: Path) -> JanitorConfig:
        """Load configuration from .janitor.json if present, else defaults."""
        config_path = repo_root / ".janitor.json"
        if not config_path.exists():
            return cls()
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
            filtered = {k: v for k, v in raw.items() if k in valid_keys}
            return cls(**filtered)
        except Exception:
            return cls()

    def save(self, repo_root: Path) -> Path:
        """Persist configuration to .janitor.json in repo_root without plaintext secrets."""
        config_path = repo_root / ".janitor.json"
        data = asdict(self)
        # Redact raw secrets from persisted repository config; users should use OPENAI_API_KEY
        if data.get("api_key"):
            data["api_key"] = ""
        config_path.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        return config_path
