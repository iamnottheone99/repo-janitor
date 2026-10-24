"""Typer CLI for Repo Janitor with Rich human tables and AXI-compliant TOON output."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from repo_janitor.config import JanitorConfig
from repo_janitor.maintainer import MaintenanceSummary, RepoMaintainer
from repo_janitor.scanner import AuditReport, RepoScanner, ScanReport
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


def _format_toon_audit(report: AuditReport) -> str:
    """Format audit findings (Errors, Gaps, Redundancies) in AXI TOON format."""
    lines = [
        f"repo: {report.repo_root.as_posix()}",
        f"summary: {len(report.errors)} errors, {len(report.gaps)} gaps, {len(report.redundancies)} redundancies across {report.total_files} files",
    ]
    for cat_name, findings in (("errors", report.errors), ("gaps", report.gaps), ("redundancies", report.redundancies)):
        if not findings:
            lines.append(f"{cat_name}: 0 findings")
        else:
            lines.append(f"{cat_name}[{len(findings)}]{{kind,file,line,conf,actionable,rec}}:")
            for f in findings:
                rel = f" -> {f.related_file}:{f.related_line}" if f.related_file else ""
                lines.append(f"  {f.kind},{f.file_path}:{f.line}{rel},{f.confidence:.2f},{str(f.is_actionable).lower()},{f.recommendation}")
    return "\n".join(lines)


@app.command("init")
def init_config(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    provider: str = typer.Option(
        "ollama",
        "--provider",
        "-p",
        help="LLM provider: ollama | openai | vllm | lmstudio",
    ),
    model: str = typer.Option("qwen2.5-coder:7b", "--model", "-m", help="Model name"),
    host: Optional[str] = typer.Option(None, "--host", help="Server URL or API base (e.g. http://localhost:1234/v1)"),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="API key for OpenAI-compliant endpoints"),
    language: str = typer.Option(
        "auto",
        "--language",
        "-l",
        help="Repository language: auto | python | typescript | javascript | go | rust",
    ),
) -> None:
    """Initialize a .janitor.json configuration file in the target repository."""
    from repo_janitor.languages import detect_repo_language, get_language_adapter

    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    cfg.provider = provider
    cfg.model = model
    if host:
        cfg.api_base = host
        cfg.ollama_host = host
    if api_key:
        cfg.api_key = api_key
    cfg.language = language
    detected = language if language != "auto" else detect_repo_language(repo_root)
    cfg.test_command = get_language_adapter(detected).default_test_command
    saved_path = cfg.save(repo_root)
    typer.echo(
        f"config: initialized {saved_path.as_posix()} "
        f"(provider={cfg.provider}, model={cfg.model}, endpoint={cfg.effective_api_base()}, language={cfg.language})"
    )


@app.command("scan")
def scan_repo(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Filter by language"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI token-efficient TOON output"),
) -> None:
    """Scan repository source and test suite for missing docstrings and uncovered functions."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    if language:
        cfg.language = language
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
        doc_table.add_column("Language")
        doc_table.add_column("Line", justify="right")
        for s in report.missing_docstrings:
            doc_table.add_row(s.file_path, s.qualified_name, s.kind, s.language, str(s.start_line))
        console.print(doc_table)
    else:
        console.print("[green]✓ All public symbols have docstrings.[/green]")

    if report.untested_functions:
        test_table = Table(title=f"Untested Functions ({len(report.untested_functions)})")
        test_table.add_column("File", style="cyan")
        test_table.add_column("Function / Method", style="magenta")
        test_table.add_column("Language")
        test_table.add_column("Signature")
        for s in report.untested_functions:
            test_table.add_row(s.file_path, s.qualified_name, s.language, s.signature)
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
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider: ollama | openai | vllm | lmstudio"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model"),
    host: Optional[str] = typer.Option(None, "--host", help="Override server URL or API base"),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Filter by language"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max tasks to run"),
    no_readme: bool = typer.Option(False, "--no-readme", help="Skip README.md sync"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI TOON output"),
) -> None:
    """Add missing docstrings and update README inside isolated `janitor/docs/*` worktrees."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    if provider:
        cfg.provider = provider
    if model:
        cfg.model = model
    if host:
        cfg.api_base = host
        cfg.ollama_host = host
    if language:
        cfg.language = language
    maintainer = RepoMaintainer(repo_root, cfg)
    summary = maintainer.run_docstring_tasks(limit=limit, include_readme=not no_readme)
    _render_summary(summary, toon=toon)


@app.command("tests")
def generate_tests(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider: ollama | openai | vllm | lmstudio"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model"),
    host: Optional[str] = typer.Option(None, "--host", help="Override server URL or API base"),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Filter by language"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max functions to test"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI TOON output"),
) -> None:
    """Generate unit tests for uncovered functions inside isolated `janitor/tests/*` worktrees."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    if provider:
        cfg.provider = provider
    if model:
        cfg.model = model
    if host:
        cfg.api_base = host
        cfg.ollama_host = host
    if language:
        cfg.language = language
    maintainer = RepoMaintainer(repo_root, cfg)
    summary = maintainer.run_unit_test_tasks(limit=limit)
    _render_summary(summary, toon=toon)


@app.command("run")
def run_all(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM provider: ollama | openai | vllm | lmstudio"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model"),
    host: Optional[str] = typer.Option(None, "--host", help="Override server URL or API base"),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Filter by language"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="Max total tasks to run"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI TOON output"),
) -> None:
    """Run full autonomous maintenance (docstrings + README + unit tests) in isolated worktrees."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    if provider:
        cfg.provider = provider
    if model:
        cfg.model = model
    if host:
        cfg.api_base = host
        cfg.ollama_host = host
    if language:
        cfg.language = language
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


def _render_audit(report: AuditReport, toon: bool = False) -> None:
    if toon:
        typer.echo(_format_toon_audit(report))
        return

    console.print(
        f"\n[bold cyan]Repo Audit:[/bold cyan] {report.repo_root} "
        f"({report.total_files} files, {report.total_symbols} symbols)"
    )
    console.print(
        f"Summary: [bold red]{len(report.errors)} errors[/bold red], "
        f"[bold yellow]{len(report.gaps)} gaps[/bold yellow], "
        f"[bold magenta]{len(report.redundancies)} redundancies[/bold magenta]\n"
    )

    if report.errors:
        err_table = Table(title="Errors (Syntax, Broken Imports, Empty Exceptions)")
        err_table.add_column("Kind", style="red")
        err_table.add_column("Location", style="cyan")
        err_table.add_column("Issue", style="yellow")
        err_table.add_column("Conf", justify="right")
        err_table.add_column("Recommendation")
        for f in report.errors:
            err_table.add_row(f.kind, f"{f.file_path}:{f.line}", f.title, f"{f.confidence:.2f}", f.recommendation)
        console.print(err_table)

    if report.gaps:
        gap_table = Table(title="Gaps (Unfinished Stubs, Untested Functions)")
        gap_table.add_column("Kind", style="yellow")
        gap_table.add_column("Location", style="cyan")
        gap_table.add_column("Target", style="magenta")
        gap_table.add_column("Conf", justify="right")
        gap_table.add_column("Recommendation")
        for f in report.gaps:
            gap_table.add_row(f.kind, f"{f.file_path}:{f.line}", f.title, f"{f.confidence:.2f}", f.recommendation)
        console.print(gap_table)

    if report.redundancies:
        red_table = Table(title="Redundancies (Code Clones, Dead Symbols)")
        red_table.add_column("Kind", style="magenta")
        red_table.add_column("Location", style="cyan")
        red_table.add_column("Details", style="yellow")
        red_table.add_column("Related File", style="blue")
        red_table.add_column("Conf", justify="right")
        red_table.add_column("Recommendation")
        for f in report.redundancies:
            related = f"{f.related_file}:{f.related_line}" if f.related_file else "-"
            red_table.add_row(f.kind, f"{f.file_path}:{f.line}", f.title, related, f"{f.confidence:.2f}", f.recommendation)
        console.print(red_table)

    if not report.total_findings:
        console.print("[green]✓ Repository is clean: 0 errors, 0 gaps, 0 redundancies found.[/green]\n")


@app.command("check")
def check_repo(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    classifier: str = typer.Option(
        "fast",
        "--classifier",
        "-c",
        help="Classifier decision model: fast | kev | ollama",
    ),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Filter by language"),
    strict: bool = typer.Option(False, "--strict", help="Exit with code 1 if errors are found (ideal for CI)"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI token-efficient TOON output"),
) -> None:
    """Audit repository for Errors, Gaps, and Duplicates/Redundancies using a decision classifier."""
    repo_root = repo.resolve()
    cfg = JanitorConfig.load(repo_root)
    cfg.classifier_model = classifier
    if language:
        cfg.language = language
    scanner = RepoScanner(repo_root, cfg)
    report = scanner.audit()
    _render_audit(report, toon=toon)
    if strict and report.errors:
        raise typer.Exit(code=1)


@app.command("audit")
def audit_repo(
    repo: Path = typer.Option(Path("."), "--repo", "-r", help="Target git repository root"),
    classifier: str = typer.Option(
        "fast",
        "--classifier",
        "-c",
        help="Classifier decision model: fast | kev | ollama",
    ),
    language: Optional[str] = typer.Option(None, "--language", "-l", help="Filter by language"),
    strict: bool = typer.Option(False, "--strict", help="Exit with code 1 if errors are found (ideal for CI)"),
    toon: bool = typer.Option(False, "--toon", help="Emit AXI token-efficient TOON output"),
) -> None:
    """Alias for 'check': audit repo for Errors, Gaps, and Redundancies."""
    check_repo(repo=repo, classifier=classifier, language=language, strict=strict, toon=toon)


if __name__ == "__main__":
    app()
