"""Configuration models and loader for Repo Janitor."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class JanitorConfig:
    """Configuration for autonomous repository maintenance runs."""

    model: str = "qwen2.5-coder:7b"
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
        """Persist configuration to .janitor.json in repo_root."""
        config_path = repo_root / ".janitor.json"
        config_path.write_text(
            json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8"
        )
        return config_path
