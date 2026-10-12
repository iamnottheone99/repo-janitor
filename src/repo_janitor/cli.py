"""Typer CLI for Repo Janitor with Rich human tables and AXI-compliant TOON output."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from repo_janitor.config import JanitorConfig
from repo_janitor.maintainer import MaintenanceSummary, RepoMaintainer
from repo_janitor.scanner import ScanReport
from repo_janitor.worktree import GitWorktreeManager

app = typer.Typer(
    name="janitor",
    help="Autonomous local Ollama-powered repository maintainer using isolated git worktrees.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _format_toon_scan(report: ScanReport) -> str:
    """Format scan findings in AXI Token-Oriented Object Notation (TOON)."""
    lines = [
        f"repo: {report.repo_root.as_posix()}",
        f"summary: {report.total_files} files, {report.total_symbols} symbols, "
        f"{len(report.missing_docstrings)} missing_docstrings, "
        f"{len(report.untested_functions)} untested_functions, "
        f"readme_needs_update={str(report.readme_needs_update).lower()}",
    ]
    if not report.missing_docstrings:
        lines.append("missing_docstrings: 0 symbols missing docstrings in this repository")
    else:
        lines.append(
            f"missing_docstrings[{len(report.missing_docstrings)}]{{file,symbol,kind,line}}:"
        )
        for s in report.missing_docstrings:
            lines.append(f"  {s.file_path},{s.qualified_name},{s.kind},{s.start_line}")

    if not report.untested_functions:
        lines.append("untested_functions: 0 uncovered functions found in this repository")
    else:
        lines.append(
            f"untested_functions[{len(report.untested_functions)}]{{file,symbol,kind,line}}:"
        )
        for s in report.untested_functions:
            lines.append(f"  {s.file_path},{s.qualified_name},{s.kind},{s.start_line}")

    return "\n".join(lines)


def _format_toon_summary(summary: MaintenanceSummary) -> str:
    """Format maintenance execution results in AXI TOON format."""
    total = len(summary.outcomes)
    if total == 0:
        return "outcomes: 0 maintenance tasks executed (repository already clean)"
    lines = [
        f"count: {summary.committed_count} committed, {summary.failed_count} failed of {total} total",
        f"outcomes[{total}]{{type,target,status,branch,commit}}:",
    ]
    for o in summary.outcomes:
        lines.append(
            f"  {o.task_type},{o.target},{o.status},{o.branch or '-'},{o.commit_sha or '-'}"
        )
    return "\n".join(lines)


@app.command("init")
def init_config(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    model: str = typer.Option("qwen2.5-coder:7b", "--model", "-m", help="Local Ollama model name"),
    host: str = typer.Option("http://localhost:11434", "--host", help="Ollama server URL"),
) -> None:
    """Initialize a .janitor.json configuration file in the target repository."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    cfg.model = model
    cfg.ollama_host = host
    saved_path = cfg.save(repo_root)
    typer.echo(f"config: initialized {saved_path.as_posix()} (model={cfg.model})")


@app.command("scan")
def scan_repo(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI token-efficient TOON output"),
) -> None:
    """Scan repository AST and test suite for missing docstrings and uncovered functions."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    maintainer = RepoMaintainer(repo_root, cfg)
    report = maintainer.scan()

    if toon:
        typer.echo(_format_toon_scan(report))
        return

    console.print(
        f"[bold cyan]Repo Scan:[/bold cyan] {report.repo_root} "
        f"({report.total_files} files, {report.total_symbols} symbols)"
    )

    if report.missing_docstrings:
        doc_table = Table(title=f"Missing Docstrings ({len(report.missing_docstrings)})")
        doc_table.add_column("File", style="cyan")
        doc_table.add_column("Symbol", style="yellow")
        doc_table.add_column("Kind")
        doc_table.add_column("Line", justify="right")
        for s in report.missing_docstrings:
            doc_table.add_row(s.file_path, s.qualified_name, s.kind, str(s.start_line))
        console.print(doc_table)
    else:
        console.print("[green]✓ All public symbols have docstrings.[/green]")

    if report.untested_functions:
        test_table = Table(title=f"Untested Functions ({len(report.untested_functions)})")
        test_table.add_column("File", style="cyan")
        test_table.add_column("Function / Method", style="magenta")
        test_table.add_column("Signature")
        for s in report.untested_functions:
            test_table.add_row(s.file_path, s.qualified_name, s.signature)
        console.print(test_table)
    else:
        console.print("[green]✓ All public functions are covered by tests.[/green]")

    if report.readme_needs_update:
        console.print(
            f"[yellow]! README.md is missing references to modules: "
            f"{', '.join(report.readme_missing_modules)}[/yellow]"
        )


@app.command("docstrings")
def generate_docstrings(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override Ollama model"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max tasks to run"),
    no_readme: bool = typer.Option(False, "--no-readme", help="Skip README.md sync"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI TOON output"),
) -> None:
    """Add missing docstrings and update README inside isolated `janitor/docs/*` worktrees."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    if model:
        cfg.model = model
    maintainer = RepoMaintainer(repo_root, cfg)
    summary = maintainer.run_docstring_tasks(limit=limit, include_readme=not no_readme)
    _render_summary(summary, toon=toon)


@app.command("tests")
def generate_tests(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override Ollama model"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max functions to test"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI TOON output"),
) -> None:
    """Generate unit tests for uncovered functions inside isolated `janitor/tests/*` worktrees."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    if model:
        cfg.model = model
    maintainer = RepoMaintainer(repo_root, cfg)
    summary = maintainer.run_unit_test_tasks(limit=limit)
    _render_summary(summary, toon=toon)


@app.command("run")
def run_all(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override Ollama model"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max total tasks to run"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI TOON output"),
) -> None:
    """Run full autonomous maintenance (docstrings + README + unit tests) in isolated worktrees."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    if model:
        cfg.model = model
    maintainer = RepoMaintainer(repo_root, cfg)
    summary = maintainer.run_all(limit=limit)
    _render_summary(summary, toon=toon)


@app.command("branches")
def list_branches(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI TOON output"),
) -> None:
    """List verified `janitor/*` branches ready for review or merge."""
    repo_root = repo.resolve()
    wt = GitWorktreeManager(repo_root)
    branches = wt.list_janitor_branches()

    if toon:
        if not branches:
            typer.echo("branches: 0 janitor branches found in this repository")
            return
        lines = [f"branches[{len(branches)}]{{branch,commit,summary}}:"]
        for b in branches:
            lines.append(f"  {b['branch']},{b['commit']},{b['summary']}")
        typer.echo("\n".join(lines))
        return

    if not branches:
        console.print("branches: 0 janitor branches found in this repository")
        return

    table = Table(title=f"Verified Janitor Branches ({len(branches)})")
    table.add_column("Branch", style="green")
    table.add_column("Commit", style="cyan")
    table.add_column("Summary")
    for b in branches:
        table.add_row(b["branch"], b["commit"], b["summary"])
    console.print(table)


@app.command("prune")
def prune_branches(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
) -> None:
    """Remove all local `janitor/*` branches and prune stale worktrees."""
    repo_root = repo.resolve()
    wt = GitWorktreeManager(repo_root)
    deleted = wt.prune_branches()
    if not deleted:
        typer.echo("prune: 0 janitor branches to delete (no-op)")
    else:
        typer.echo(f"prune: deleted {len(deleted)} janitor branch(es): {', '.join(deleted)}")


def _render_summary(summary: MaintenanceSummary, toon: bool = False) -> None:
    if toon:
        typer.echo(_format_toon_summary(summary))
        return

    if not summary.outcomes:
        console.print("[green]outcomes: 0 maintenance tasks needed (repository is clean)[/green]")
        return

    table = Table(
        title=f"Maintenance Outcomes ({summary.committed_count} committed, {summary.failed_count} failed)"
    )
    table.add_column("Type", style="cyan")
    table.add_column("Target", style="yellow")
    table.add_column("Status")
    table.add_column("Branch", style="green")
    table.add_column("Commit")
    table.add_column("Detail")

    for o in summary.outcomes:
        status_styled = (
            f"[green]{o.status}[/green]"
            if o.status == "committed"
            else f"[red]{o.status}[/red]"
        )
        table.add_row(
            o.task_type,
            o.target,
            status_styled,
            o.branch or "-",
            o.commit_sha or "-",
            o.detail,
        )
    console.print(table)


if __name__ == "__main__":
    app()
