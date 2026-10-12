"""Isolated Git worktree and branch manager for safe autonomous maintenance."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from git import Repo

from repo_janitor.config import JanitorConfig


@dataclass
class VerificationResult:
    """Outcome of running the repository test suite inside an isolated worktree."""

    passed: bool
    exit_code: int
    stdout: str
    stderr: str

    @property
    def combined_output(self) -> str:
        return f"{self.stdout}\n{self.stderr}".strip()


class GitWorktreeManager:
    """Manages isolated git worktrees and `janitor/<task>` branches."""

    def __init__(self, repo_root: Path, config: JanitorConfig | None = None) -> None:
        self.repo_root = repo_root.resolve()
        self.config = config or JanitorConfig.load(self.repo_root)
        self.repo = Repo(self.repo_root)

    def make_branch_name(self, category: str, target_slug: str) -> str:
        """Build a clean git branch name like `janitor/tests/math_utils-add`."""
        clean_slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", target_slug).strip("-").lower()
        return f"{self.config.branch_prefix}/{category}/{clean_slug}"

    def list_janitor_branches(self) -> list[dict[str, str]]:
        """Return metadata for all existing `janitor/*` branches in the repository."""
        prefix = f"{self.config.branch_prefix}/"
        branches: list[dict[str, str]] = []
        for head in self.repo.heads:
            if head.name.startswith(prefix):
                commit = head.commit
                branches.append(
                    {
                        "branch": head.name,
                        "commit": commit.hexsha[:8],
                        "summary": commit.summary,
                        "authored_date": commit.authored_datetime.isoformat(),
                    }
                )
        return branches

    @contextmanager
    def isolated_worktree(
        self, category: str, target_slug: str
    ) -> Iterator[tuple[Path, str]]:
        """Create an isolated git worktree on a new branch and clean up afterward."""
        branch_name = self.make_branch_name(category, target_slug)
        wt_base = (self.repo_root / self.config.worktree_dir).resolve()
        wt_base.mkdir(parents=True, exist_ok=True)

        safe_dir_name = branch_name.replace("/", "__")
        wt_path = wt_base / safe_dir_name

        # Clean up any leftover worktree or branch from an interrupted previous run
        self._cleanup_worktree(wt_path)
        self._delete_branch_if_exists(branch_name)

        self.repo.git.worktree("add", "-b", branch_name, str(wt_path), "HEAD")
        keep_branch = False
        try:
            yield wt_path, branch_name
            # Keep the branch only if a new commit was recorded on it
            head_sha = self.repo.head.commit.hexsha
            branch_sha = self.repo.heads[branch_name].commit.hexsha
            keep_branch = branch_sha != head_sha
        finally:
            self._cleanup_worktree(wt_path)
            if not keep_branch:
                self._delete_branch_if_exists(branch_name)

    def run_tests(self, worktree_path: Path) -> VerificationResult:
        """Execute the configured test suite inside the isolated worktree."""
        env = os.environ.copy()
        src_dir = worktree_path / "src"
        py_paths = [str(src_dir), str(worktree_path)] if src_dir.exists() else [str(worktree_path)]
        existing_py_path = env.get("PYTHONPATH", "")
        if existing_py_path:
            py_paths.append(existing_py_path)
        env["PYTHONPATH"] = os.pathsep.join(py_paths)

        cmd = self.config.test_command
        # Ensure pytest runs under the active Python interpreter for reliability
        if cmd.startswith("pytest"):
            cmd = f'"{sys.executable}" -m {cmd}'

        proc = subprocess.run(
            cmd,
            cwd=str(worktree_path),
            env=env,
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return VerificationResult(
            passed=(proc.returncode == 0),
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    def commit_changes(
        self, worktree_path: Path, files: list[str], message: str
    ) -> str:
        """Stage specified files and commit them in the isolated worktree."""
        wt_repo = Repo(worktree_path)
        wt_repo.index.add(files)
        commit = wt_repo.index.commit(message)
        return commit.hexsha[:8]

    def prune_branches(self) -> list[str]:
        """Delete all local `janitor/*` branches and prune stale worktrees."""
        self.repo.git.worktree("prune")
        prefix = f"{self.config.branch_prefix}/"
        deleted: list[str] = []
        active_branch = self.repo.active_branch.name if not self.repo.head.is_detached else ""
        for head in list(self.repo.heads):
            if head.name.startswith(prefix) and head.name != active_branch:
                self.repo.git.branch("-D", head.name)
                deleted.append(head.name)
        return deleted

    def _cleanup_worktree(self, wt_path: Path) -> None:
        if wt_path.exists():
            try:
                self.repo.git.worktree("remove", "--force", str(wt_path))
            except Exception:
                shutil.rmtree(wt_path, ignore_errors=True)
                try:
                    self.repo.git.worktree("prune")
                except Exception:
                    pass

    def _delete_branch_if_exists(self, branch_name: str) -> None:
        if branch_name in [h.name for h in self.repo.heads]:
            try:
                self.repo.git.branch("-D", branch_name)
            except Exception:
                pass
