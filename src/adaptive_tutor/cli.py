"""Polished Adaptive Tutor command-line interface."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

import typer
import uvicorn
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from . import __version__
from .assignments import AssignmentService
from .codex import CodexRunner
from .config import (
    DEFAULT_CONFIG_PATH,
    TutorSettings,
    load_settings,
    write_initial_config,
)
from .curriculum import CurriculumLoader, bundled_curriculum_path
from .dashboard import create_app
from .db import Database
from .demo import run_demo
from .doctor import Doctor
from .errors import TutorError
from .evaluation import EvaluationService
from .github import GitHubClient
from .jobs import JobQueue, Worker
from .learner import LearnerModel
from .models import LearnerContext
from .orchestrator import TutorOrchestrator
from .reporting import ReportService
from .scheduler import AdaptiveScheduler
from .state import StatusService

app = typer.Typer(
    name="adaptive-tutor",
    help="A self-hosted, Git-native adaptive learning engine.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
console = Console()


class CLIContext:
    def __init__(self, config_path: Path | None) -> None:
        self.config_path = config_path


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        envvar="ADAPTIVE_TUTOR_CONFIG",
        help="Configuration file (default: platform user config directory).",
        dir_okay=False,
    ),
    version: bool = typer.Option(
        False, "--version", help="Show the installed version and exit.", is_eager=True
    ),
) -> None:
    if version:
        console.print(f"adaptive-tutor {__version__}")
        raise typer.Exit()
    ctx.obj = CLIContext(config)


@app.command("init")
def init_command(
    ctx: typer.Context,
    data_dir: Path | None = typer.Option(
        None, help="Private state directory; defaults to the platform data directory."
    ),
    github_owner: str = typer.Option("", help="github.com account that owns private repositories."),
    workspace_repo: str = typer.Option("learning-workspace", help="Private learner repository."),
    curriculum_repo: str = typer.Option("private-curricula", help="Private curriculum repository."),
    app_id: int | None = typer.Option(None, help="Least-privilege GitHub App identifier."),
    installation_id: int | None = typer.Option(None, help="GitHub App installation identifier."),
    private_key: Path | None = typer.Option(None, help="Owner-only GitHub App private-key file."),
    webhook_url: str | None = typer.Option(None, help="Public HTTPS service base URL."),
    server_host: str = typer.Option(
        "127.0.0.1",
        help="Dashboard bind host; use 0.0.0.0 only behind a loopback-bound container port.",
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing configuration."),
) -> None:
    """Create secure defaults, migrate SQLite, and load the demo curriculum."""
    context = _context(ctx)
    path = context.config_path or DEFAULT_CONFIG_PATH
    try:
        config_path, secrets_path = write_initial_config(
            path,
            force=force,
            data_dir=data_dir,
            github_owner=github_owner,
            workspace_repo=workspace_repo,
            curriculum_repo=curriculum_repo,
            app_id=app_id,
            installation_id=installation_id,
            private_key_path=private_key,
            webhook_url=webhook_url,
            server_host=server_host,
        )
        settings = load_settings(config_path, require_file=True)
        database = _bootstrap(settings, force_load=True)
    except (TutorError, ValueError, OSError) as exc:
        _abort(str(exc))
    table = Table(box=box.ROUNDED, show_header=False, pad_edge=True)
    table.add_column(style="bold green")
    table.add_column()
    table.add_row("✓ Configuration", str(config_path))
    table.add_row("✓ Secrets", f"{secrets_path} (mode 0600)")
    table.add_row("✓ Database", str(database.path))
    table.add_row("✓ Curriculum", settings.active_curriculum)
    console.print(
        Panel(table, title="[bold]Adaptive Tutor initialized[/bold]", border_style="green")
    )
    if github_owner and not app_id:
        console.print(
            "[yellow]GitHub owner saved; configure a GitHub App before remote assignments.[/yellow]"
        )
    console.print("Next: [bold]adaptive-tutor doctor[/bold], then [bold]adaptive-tutor demo[/bold]")


@app.command()
def doctor(
    ctx: typer.Context,
    offline: bool = typer.Option(False, help="Skip external connectivity checks."),
    strict: bool = typer.Option(False, help="Treat warnings as a failing exit status."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Validate configuration, database, Codex, GitHub, tooling, and service health."""
    settings, database = _runtime(ctx)
    checks = Doctor(settings, database).run(online=not offline)
    if json_output:
        console.print_json(data=[item.__dict__ for item in checks])
    else:
        table = Table(box=box.ROUNDED, title="Adaptive Tutor doctor", expand=True)
        table.add_column("Check", style="bold")
        table.add_column("State", width=8)
        table.add_column("Detail")
        for check in checks:
            state = {
                "pass": "[green]✓ pass[/green]",
                "warn": "[yellow]! warn[/yellow]",
                "fail": "[red]✗ fail[/red]",
            }[check.status]
            detail = check.detail + (f"\n[dim]Fix: {check.fix}[/dim]" if check.fix else "")
            table.add_row(check.name, state, detail)
        console.print(table)
    failed = any(item.status == "fail" or (strict and item.status == "warn") for item in checks)
    if failed:
        raise typer.Exit(1)


@app.command()
def status(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the active assignment, readiness, weak spots, and runtime state."""
    settings, database = _runtime(ctx)
    snapshot = StatusService(database).get_status(
        settings.learner_id, settings.active_curriculum
    )
    if json_output:
        console.print_json(data=snapshot.model_dump(mode="json"))
        return
    active = snapshot.active_assignment
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row(
        "Runtime",
        "[yellow]paused[/yellow]" if snapshot.paused else "[green]✓ active[/green]",
    )
    grid.add_row("Curriculum", snapshot.active_curriculum)
    grid.add_row("Assignment", f"{active['id']} · {active['title']}" if active else "none")
    if active:
        grid.add_row("Progress", f"{active['status']} · stage {active['current_stage']}")
    grid.add_row("Reviews due", str(_due_count(snapshot.upcoming_reviews)))
    grid.add_row("Active misconceptions", str(len(snapshot.misconceptions)))
    grid.add_row("Model cost", f"${float(snapshot.model_usage['cost_usd']):.4f}")
    console.print(Panel(grid, title="[bold]Adaptive Tutor[/bold]", border_style="green"))
    _print_readiness(snapshot.readiness)


@app.command("next")
def next_assignment(
    ctx: typer.Context,
    available_minutes: int = typer.Option(45, min=5, max=480, help="Time available now."),
    energy: Literal["low", "medium", "high"] = typer.Option("medium"),
    days_until_goal: int | None = typer.Option(None, min=0, help="Optional scheduling horizon."),
    dry_run: bool = typer.Option(False, help="Show the recommendation without creating a PR."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Select and create the next adaptive assignment."""
    settings, database = _runtime(ctx)
    context = LearnerContext(
        available_minutes=available_minutes,
        energy=energy,
        days_until_goal=days_until_goal,
    )
    if dry_run:
        candidates = AdaptiveScheduler(database).recommend(
            settings.learner_id,
            settings.active_curriculum,
            settings.active_profile,
            context,
            limit=3,
        )
        payload = [item.model_dump(mode="json") for item in candidates]
        if json_output:
            console.print_json(data=payload)
        else:
            table = Table("Concept", "Format", "Difficulty", "Priority", "Why", box=box.ROUNDED)
            for item in candidates:
                table.add_row(
                    item.concept_id,
                    item.exercise_type.value.replace("_", " "),
                    str(item.target_difficulty),
                    f"{item.priority:.2f}",
                    item.reason,
                )
            console.print(table)
        return
    orchestrator = _orchestrator(settings, database)
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True
        ) as progress:
            progress.add_task("Validating and publishing the next assignment…", total=None)
            created = orchestrator.create_next_assignment(context)
    except (TutorError, ValueError) as exc:
        _abort(str(exc))
    if json_output:
        console.print_json(data=_json_safe(created))
    elif created.get("existing"):
        console.print(f"[yellow]Current assignment remains active:[/yellow] {created['title']}")
    else:
        console.print(
            Panel(
                f"[bold]{created['id']}: {created['title']}[/bold]\n"
                f"Branch: {created['branch_name']}\nPR: {created['url']}",
                title="Assignment created",
                border_style="green",
            )
        )


@app.command()
def current(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the current assignment without exposing hidden evaluator material."""
    settings, database = _runtime(ctx)
    active = AssignmentService(database).active(settings.learner_id)
    if active is None:
        console.print("No active assignment. Run [bold]adaptive-tutor next[/bold].")
        return
    public = {key: value for key, value in active.items() if key != "bundle"}
    if json_output:
        console.print_json(data=_json_safe(public))
        return
    table = Table.grid(padding=(0, 2))
    for label, key in (
        ("ID", "id"),
        ("Title", "title"),
        ("Status", "status"),
        ("Format", "exercise_type"),
        ("Difficulty", "difficulty"),
        ("Expected", "expected_minutes"),
        ("Branch", "branch_name"),
        ("Pull request", "pull_number"),
        ("Stage", "current_stage"),
    ):
        table.add_row(f"[bold]{label}[/bold]", str(public.get(key) or "—"))
    console.print(Panel(table, title="Current assignment"))


@app.command()
def hint(ctx: typer.Context) -> None:
    """Reveal the next progressive hint and record its use as evidence."""
    settings, database = _runtime(ctx)
    active = AssignmentService(database).active(settings.learner_id)
    if active is None:
        _abort("No active assignment. Run 'adaptive-tutor next' first.")
    level, content = AssignmentService(database).next_hint(
        str(active["id"]), settings.learner_id
    )
    console.print(Panel(content, title=f"Hint {level}/5", border_style="yellow"))


@app.command()
def readiness(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show readiness and uncertainty by curriculum domain."""
    settings, database = _runtime(ctx)
    domains = LearnerModel(database).readiness(settings.learner_id, settings.active_curriculum)
    if json_output:
        console.print_json(data=domains)
    else:
        _print_readiness(domains)


@app.command()
def report(
    ctx: typer.Context,
    period: Literal["weekly", "monthly"] = typer.Option("weekly"),
    format: Literal["console", "markdown", "json"] = typer.Option("console", "--format"),
    output: Path | None = typer.Option(None, help="Write Markdown or JSON to this file."),
) -> None:
    """Generate a polished weekly or monthly progress report."""
    settings, database = _runtime(ctx)
    document = ReportService(database).generate(
        settings.learner_id, settings.active_curriculum, period
    )
    if format == "json":
        rendered = json.dumps(document.data, indent=2, sort_keys=True)
    else:
        rendered = document.markdown
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        console.print(f"Report written to [bold]{output}[/bold]")
    elif format == "json":
        console.print_json(rendered)
    elif format == "markdown":
        console.print(rendered, markup=False)
    else:
        console.print(Panel(document.markdown, title=f"{period.title()} report"))


@app.command()
def history(
    ctx: typer.Context,
    limit: int = typer.Option(20, min=1, max=200),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List assignment, attempt, and score history."""
    settings, database = _runtime(ctx)
    rows = StatusService(database).history(settings.learner_id, limit=limit)
    if json_output:
        console.print_json(data=rows)
        return
    table = Table("ID", "Assignment", "Format", "Diff", "Status", "Attempts", "Score")
    for row in rows:
        table.add_row(
            str(row["id"]),
            str(row["title"]),
            str(row["exercise_type"]).replace("_", " "),
            str(row["difficulty"]),
            str(row["status"]),
            str(row["attempts"]),
            f"{float(row['score']):.0f}" if row["score"] is not None else "—",
        )
    console.print(table)


@app.command()
def concepts(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Inspect mastery, evidence, review, trend, and calibration by concept."""
    settings, database = _runtime(ctx)
    rows = StatusService(database).concepts(settings.learner_id, settings.active_curriculum)
    if json_output:
        console.print_json(data=rows)
        return
    table = Table("Domain", "Concept", "Mastery", "Uncertainty", "Evidence", "Trend")
    for row in rows:
        trend = float(row["trend"])
        table.add_row(
            str(row["domain"]),
            str(row["name"]),
            f"{float(row['mastery_estimate']):.0%}",
            f"{float(row['uncertainty']):.0%}",
            str(row["evidence_count"]),
            f"{trend:+.0%}",
        )
    console.print(table)


@app.command()
def pause(ctx: typer.Context) -> None:
    """Pause adaptive assignment creation without stopping evaluation."""
    _, database = _runtime(ctx)
    StatusService(database).set_paused(True)
    console.print("[yellow]Adaptive assignment creation paused.[/yellow]")


@app.command()
def resume(ctx: typer.Context) -> None:
    """Resume adaptive assignment creation."""
    _, database = _runtime(ctx)
    StatusService(database).set_paused(False)
    console.print("[green]Adaptive assignment creation resumed.[/green]")


@app.command()
def demo(
    keep: Path | None = typer.Option(None, help="Keep demo state in this directory."),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run a realistic local flow with neutral data and no credentials."""
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True
        ) as progress:
            task = progress.add_task("Running adaptive scheduling…", total=None)
            result = run_demo(keep)
            progress.update(task, description="Validating evidence and updating learner state…")
    except (TutorError, ValueError, OSError) as exc:
        _abort(str(exc))
    if json_output:
        console.print_json(
            data={
                "curriculum": result.curriculum,
                "recommendation": result.recommendation,
                "assignment": result.assignment,
                "validation_checks": result.validation_checks,
                "automated_evidence": result.automated_evidence,
                "qualitative_evaluation": result.qualitative_evaluation,
                "status": result.status,
                "report": result.report.data,
            }
        )
        return
    stages = Table.grid(padding=(0, 2))
    stages.add_column(width=3, style="green bold")
    stages.add_column(style="bold")
    stages.add_column(style="dim")
    stages.add_row("1", "Curriculum loaded", result.curriculum)
    stages.add_row(
        "2",
        "Scheduler selected",
        f"{result.recommendation['concept_id']} · difficulty "
        f"{result.recommendation['target_difficulty']}",
    )
    stages.add_row("3", "Assignment validated", result.assignment["title"])
    stages.add_row(
        "4",
        "Deterministic evidence",
        f"{len(result.automated_evidence['checks'])} checks passed",
    )
    stages.add_row(
        "5",
        "Structured review",
        f"{result.qualitative_evaluation['overall_score']:.0f}/100",
    )
    stages.add_row("6", "Learner model updated", "transaction committed")
    stages.add_row("7", "Progress report", "weekly Markdown + structured data")
    console.print(
        Panel(
            stages,
            title="[bold]Adaptive Tutor · local demo[/bold]",
            border_style="green",
        )
    )
    console.print(
        f"[bold]Next recommendation:[/bold] {result.recommendation['reason']}\n"
        "[dim]No GitHub credentials, private curriculum, network calls, or live model "
        "were used.[/dim]"
    )
    if keep:
        console.print(f"Demo state kept at {keep}")


@app.command(hidden=True)
def serve(ctx: typer.Context) -> None:
    """Run the webhook, API, and private dashboard service."""
    settings, database = _runtime(ctx)
    orchestrator = _orchestrator(settings, database) if settings.github.owner else None
    application = create_app(settings, database, orchestrator)
    uvicorn.run(
        application,
        host=settings.server.host,
        port=settings.server.port,
        log_level="info",
        access_log=False,
    )


@app.command(hidden=True)
def worker(
    ctx: typer.Context,
    once: bool = typer.Option(False, help="Process at most one queued job."),
) -> None:
    """Run the persistent event worker."""
    settings, database = _runtime(ctx)
    orchestrator = _orchestrator(settings, database)
    runner = Worker(
        JobQueue(database),
        orchestrator.handlers(),
        lease_seconds=settings.server.lease_seconds,
    )
    if once:
        runner.run_once()
        return
    console.print("Adaptive Tutor worker started. Press Ctrl-C to stop.")
    try:
        while True:
            if not runner.run_once():
                time.sleep(1)
    except KeyboardInterrupt:
        console.print("Worker stopped.")


@app.command()
def backup(
    ctx: typer.Context,
    destination: Path | None = typer.Argument(None, help="Backup file path."),
) -> None:
    """Create an online, integrity-safe SQLite backup."""
    settings, database = _runtime(ctx)
    target = destination or (
        settings.data_dir
        / "backups"
        / f"tutor-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.sqlite3"
    )
    database.backup(target)
    console.print(f"[green]Backup complete:[/green] {target}")


@app.command()
def restore(
    ctx: typer.Context,
    source: Path = typer.Argument(..., exists=True, dir_okay=False),
    yes: bool = typer.Option(False, "--yes", help="Confirm replacement of current state."),
) -> None:
    """Restore a verified SQLite backup (stop services first)."""
    if not yes:
        _abort("Restore replaces current state. Stop services and rerun with --yes.")
    _, database = _runtime(ctx)
    database.restore(source)
    healthy, detail = database.integrity_check()
    if not healthy:
        _abort(f"Restored database failed integrity check: {detail}")
    console.print("[green]Restore complete; integrity check passed.[/green]")


@app.command("curriculum-load")
def curriculum_load(ctx: typer.Context, path: Path = typer.Argument(..., exists=True)) -> None:
    """Validate and load a curriculum package without changing engine code."""
    settings, database = _runtime(ctx)
    try:
        package = CurriculumLoader().load(path)
        CurriculumLoader().persist(package, database, settings.learner_id)
    except (TutorError, ValueError) as exc:
        _abort(str(exc))
    console.print(
        f"[green]Loaded[/green] {package.metadata.name} {package.metadata.version} "
        f"({len(package.concepts)} concepts)"
    )


@app.command("webhook-setup")
def webhook_setup(ctx: typer.Context) -> None:
    """Create or reconcile the signed GitHub repository webhook."""
    settings, _ = _runtime(ctx)
    if not settings.github.webhook_url or not settings.webhook_secret:
        _abort("Configure github.webhook_url and a webhook secret first.")
    client = GitHubClient(settings.github)
    try:
        hook_id = client.create_or_verify_webhook(
            settings.github.webhook_url + "/webhooks/github", settings.webhook_secret
        )
    except TutorError as exc:
        _abort(str(exc))
    finally:
        client.close()
    console.print(f"[green]Webhook active:[/green] {hook_id}")


def _context(ctx: typer.Context) -> CLIContext:
    value = ctx.obj
    if not isinstance(value, CLIContext):
        return CLIContext(None)
    return value


def _runtime(ctx: typer.Context) -> tuple[TutorSettings, Database]:
    path = _context(ctx).config_path
    try:
        settings = load_settings(path, require_file=True)
        return settings, _bootstrap(settings)
    except (TutorError, ValueError, OSError) as exc:
        _abort(str(exc))


def _bootstrap(settings: TutorSettings, *, force_load: bool = False) -> Database:
    if settings.database_path is None:  # pragma: no cover - model invariant
        raise ValueError("database_path is not configured")
    database = Database(settings.database_path)
    database.migrate()
    active = database.fetch_one(
        "SELECT id FROM curricula WHERE id=?", (settings.active_curriculum,)
    )
    if force_load or active is None:
        paths = [bundled_curriculum_path(), *settings.curriculum_paths]
        loaded = False
        for path in paths:
            package = CurriculumLoader().load(path)
            CurriculumLoader().persist(package, database, settings.learner_id)
            loaded |= package.metadata.id == settings.active_curriculum
        if not loaded:
            raise ValueError(
                f"Active curriculum '{settings.active_curriculum}' was not found in "
                "configured paths"
            )
    return database


def _orchestrator(settings: TutorSettings, database: Database) -> TutorOrchestrator:
    github = GitHubClient(settings.github)
    evaluator = EvaluationService(database, CodexRunner(settings.codex, database))
    return TutorOrchestrator(settings, database, github, evaluator)


def _print_readiness(domains: list[Any]) -> None:
    table = Table("Domain", "Readiness", "Uncertainty", "Concepts", box=box.ROUNDED)
    for item in domains:
        values = item.model_dump() if hasattr(item, "model_dump") else item
        readiness_value = float(values["readiness"])
        color = "green" if readiness_value >= 0.7 else "yellow" if readiness_value >= 0.4 else "red"
        table.add_row(
            str(values["domain"]).replace("-", " ").title(),
            f"[{color}]{readiness_value:.0%}[/{color}]",
            f"{float(values['uncertainty']):.0%}",
            str(values["concept_count"]),
        )
    console.print(table)


def _due_count(reviews: list[dict[str, Any]]) -> int:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    return sum(str(item["next_review"]) <= now for item in reviews)


def _json_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items() if key != "bundle"}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _abort(message: str) -> NoReturn:
    console.print(Panel(Text(message), title="[red]Adaptive Tutor error[/red]", border_style="red"))
    raise typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
