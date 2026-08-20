"""Polished Adaptive Tutor command-line interface."""

from __future__ import annotations

import json
import os
import socket as socket_module
import stat
import time
from datetime import UTC, date, datetime
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
    CodexSettings,
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
from .goals import GoalService, LearningGoal
from .grader import create_grader_app
from .jobs import JobQueue, Worker
from .learner import LearnerModel
from .models import LearnerContext
from .orchestrator import TutorOrchestrator
from .reporting import ReportDocument, ReportService
from .runner import evaluate_public_workspace_to_file
from .scheduler import AdaptiveScheduler
from .state import StatusService

app = typer.Typer(
    name="adaptive-tutor",
    help="A self-hosted, Git-native adaptive learning engine.",
    no_args_is_help=True,
    invoke_without_command=True,
    rich_markup_mode="markdown",
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)
goal_app = typer.Typer(help="Manage durable learning goals.", no_args_is_help=True)
app.add_typer(goal_app, name="goal")
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
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show model and domain detail."),
) -> None:
    """Show what to work on now and the evidence behind that choice."""
    settings, database = _runtime(ctx)
    snapshot = StatusService(database).get_status(settings.learner_id, settings.active_curriculum)
    if json_output:
        console.print_json(data=snapshot.model_dump(mode="json"))
        return
    active = snapshot.active_assignment
    publication_error = str((active or {}).get("publication_error") or "")
    state = "PAUSED" if snapshot.paused else "ACTION REQUIRED" if publication_error else "READY"
    state_style = "yellow" if snapshot.paused or publication_error else "green"
    console.print(f"[bold]Adaptive Tutor[/bold]  [{state_style}]{state}[/{state_style}]")
    console.print()
    if active:
        console.print(f"[green bold]CURRENT · {active['id']}[/green bold]")
        console.print(f"[bold]{active['title']}[/bold]")
        console.print(
            f"{str(active['exercise_type']).replace('_', ' ').title()} · "
            f"{active['expected_minutes']} min · difficulty {active['difficulty']}/10"
        )
        if active.get("selection_reason"):
            console.print(f"[dim]Why now:[/dim] {active['selection_reason']}")
        if publication_error:
            console.print(f"[yellow]Publication paused:[/yellow] {publication_error}")
            console.print("[yellow]Retry:[/yellow] adaptive-tutor next")
        location_label, location = _assignment_location(settings, database, active)
        console.print(f"[green]{location_label}:[/green] {location}")
    else:
        candidates = AdaptiveScheduler(database).recommend(
            settings.learner_id,
            settings.active_curriculum,
            settings.active_profile,
            LearnerContext(),
            limit=1,
        )
        if candidates:
            candidate = candidates[0]
            name = _concept_names(database).get(candidate.concept_id, candidate.concept_id)
            console.print("[green bold]NEXT RECOMMENDATION[/green bold]")
            console.print(f"[bold]{name}[/bold]")
            console.print(
                f"{candidate.exercise_type.value.replace('_', ' ').title()} · "
                f"difficulty {candidate.target_difficulty}/10"
            )
            console.print(f"[dim]Why now:[/dim] {candidate.reason}")
            console.print("[green]Start:[/green] adaptive-tutor next")
        else:
            console.print("No schedulable concept is available for the current profile.")
    assessed = sum(item.assessed_concept_count for item in snapshot.readiness)
    total = sum(item.concept_count for item in snapshot.readiness)
    console.print()
    console.print(
        f"[dim]{_due_count(snapshot.upcoming_reviews)} reviews due · "
        f"{len(snapshot.misconceptions)} open misconceptions · "
        f"{assessed}/{total} concepts assessed[/dim]"
    )
    if verbose:
        console.print(
            f"[dim]Curriculum:[/dim] {snapshot.active_curriculum}  "
            f"[dim]Model cost:[/dim] ${float(snapshot.model_usage['cost_usd']):.4f}"
        )
        _print_readiness(snapshot.readiness, verbose=True)


@goal_app.command("show")
def goal_show(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the active learning goal."""
    settings, database = _runtime(ctx)
    goal = GoalService(database).active(settings.learner_id, settings.active_curriculum)
    if json_output:
        console.print_json(data=goal.model_dump(mode="json") if goal else None)
    elif goal is None:
        console.print("No active learning goal is set.")
    else:
        _print_goal(goal)


@goal_app.command("set")
def goal_set(
    ctx: typer.Context,
    statement: str = typer.Argument(..., help="The learning outcome to pursue."),
    target_date: str | None = typer.Option(
        None, "--target-date", metavar="YYYY-MM-DD", help="Optional completion date."
    ),
    domains: list[str] | None = typer.Option(
        None, "--domain", help="Curriculum domain to prioritize; repeatable."
    ),
    concepts: list[str] | None = typer.Option(
        None, "--concept", help="Curriculum concept to prioritize; repeatable."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Set or revise the active learning goal."""
    settings, database = _runtime(ctx)
    parsed_target = _parse_goal_date(target_date)
    try:
        goal = GoalService(database).set(
            settings.learner_id,
            settings.active_curriculum,
            settings.active_profile,
            statement,
            target_date=parsed_target,
            focus_domains=domains,
            focus_concepts=concepts,
        )
    except ValueError as exc:
        _abort(str(exc))
    if json_output:
        console.print_json(data=goal.model_dump(mode="json"))
    else:
        _print_goal(goal)


@goal_app.command("history")
def goal_history(
    ctx: typer.Context,
    limit: int = typer.Option(20, min=1, max=100, help="Maximum revisions to show."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show learning goal revision history."""
    settings, database = _runtime(ctx)
    goals = GoalService(database).history(
        settings.learner_id, settings.active_curriculum, limit=limit
    )
    if json_output:
        console.print_json(data=[goal.model_dump(mode="json") for goal in goals])
        return
    if not goals:
        console.print("No learning goal history is available.")
        return
    table = Table("Revision", "Status", "Goal", "Target", "Focus", box=box.SIMPLE)
    for goal in goals:
        focus = ", ".join([*goal.focus_domains, *goal.focus_concepts]) or "all concepts"
        table.add_row(
            str(goal.revision),
            goal.status.value,
            Text(goal.statement),
            goal.target_date.isoformat() if goal.target_date else "none",
            focus,
        )
    console.print(table)


@app.command("next")
def next_assignment(
    ctx: typer.Context,
    available_minutes: int = typer.Option(45, min=5, max=480, help="Time available now."),
    energy: Literal["low", "medium", "high"] = typer.Option("medium"),
    days_until_goal: int | None = typer.Option(None, min=0, help="Optional scheduling horizon."),
    dry_run: bool = typer.Option(False, help="Show the recommendation without creating a PR."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show ranked alternatives."),
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
            limit=3 if verbose or json_output else 1,
        )
        payload = [item.model_dump(mode="json") for item in candidates]
        if json_output:
            console.print_json(data=payload)
        elif not candidates:
            console.print("No schedulable concept matches this session context.")
        else:
            active = AssignmentService(database).active(settings.learner_id)
            if active:
                console.print(
                    f"[yellow]Current work remains active:[/yellow] "
                    f"{active['id']} · {active['title']}\n"
                )
            names = _concept_names(database)
            first = candidates[0]
            console.print("[green bold]NEXT RECOMMENDATION[/green bold]")
            console.print(f"[bold]{names.get(first.concept_id, first.concept_id)}[/bold]")
            console.print(
                f"{first.exercise_type.value.replace('_', ' ').title()} · "
                f"difficulty {first.target_difficulty}/10 · "
                f"fits a {available_minutes} min session"
            )
            console.print(f"[dim]Why now:[/dim] {first.reason}")
            if verbose and len(candidates) > 1:
                table = Table("Rank", "Concept", "Format", "Diff", "Priority", box=box.SIMPLE)
                for rank, item in enumerate(candidates, 1):
                    table.add_row(
                        str(rank),
                        names.get(item.concept_id, item.concept_id),
                        item.exercise_type.value.replace("_", " "),
                        str(item.target_difficulty),
                        f"{item.priority:.2f}",
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
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show the current assignment without exposing hidden evaluator material."""
    settings, database = _runtime(ctx)
    active = AssignmentService(database).active(settings.learner_id)
    if active is None:
        console.print("No active assignment. Run [bold]adaptive-tutor next[/bold].")
        return
    bundle = active["bundle"]
    public = {key: value for key, value in active.items() if key != "bundle"}
    if json_output:
        console.print_json(data=_json_safe(public))
        return
    console.print(f"[green bold]CURRENT · {public['id']}[/green bold]")
    console.print(f"[bold]{public['title']}[/bold]")
    console.print(str(bundle.summary))
    console.print(
        f"{str(public['exercise_type']).replace('_', ' ').title()} · "
        f"{public['expected_minutes']} min · difficulty {public['difficulty']}/10"
    )
    if bundle.selection_reason:
        console.print(f"[dim]Why now:[/dim] {bundle.selection_reason}")
    if public.get("publication_error"):
        console.print(f"[yellow]Publication paused:[/yellow] {public['publication_error']}")
        console.print("[yellow]Retry:[/yellow] adaptive-tutor next")
    location_label, location = _assignment_location(settings, database, public)
    console.print(f"[green]{location_label}:[/green] {location}")
    if verbose:
        console.print(
            f"[dim]Status:[/dim] {public['status']}  [dim]Stage:[/dim] "
            f"{public['current_stage']}  [dim]Branch:[/dim] {public['branch_name']}"
        )
        files = [item.path for item in bundle.files if item.role not in {"reference", "evaluator"}]
        console.print(f"[dim]Public files:[/dim] {', '.join(files)}")


@app.command()
def hint(ctx: typer.Context) -> None:
    """Reveal the next progressive hint and record its use as evidence."""
    settings, database = _runtime(ctx)
    active = AssignmentService(database).active(settings.learner_id)
    if active is None:
        _abort("No active assignment. Run 'adaptive-tutor next' first.")
    level, content = AssignmentService(database).next_hint(str(active["id"]), settings.learner_id)
    console.print(Panel(content, title=f"Hint {level}/5", border_style="yellow"))


@app.command()
def readiness(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show readiness and uncertainty by curriculum domain."""
    settings, database = _runtime(ctx)
    domains = LearnerModel(database).readiness(settings.learner_id, settings.active_curriculum)
    if json_output:
        console.print_json(data=domains)
    else:
        _print_readiness(domains, verbose=verbose)


@app.command()
def report(
    ctx: typer.Context,
    period: Literal["weekly", "monthly"] = typer.Option("weekly"),
    format: Literal["console", "markdown", "json"] = typer.Option("console", "--format"),
    output: Path | None = typer.Option(None, help="Write Markdown or JSON to this file."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show operational totals."),
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
        _print_console_report(document, verbose=verbose)


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
def review(
    ctx: typer.Context,
    assignment_id: str | None = typer.Argument(
        None, help="Assignment ID; defaults to the latest completed review."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show complete feedback for the latest or selected assignment review."""
    settings, database = _runtime(ctx)
    projection = StatusService(database).review(
        settings.learner_id,
        assignment_id,
        github_owner=settings.github.owner,
        workspace_repo=settings.github.workspace_repo,
    )
    if projection is None:
        target = f" for assignment '{assignment_id}'" if assignment_id else ""
        _abort(f"No completed review is available{target}.")
    if json_output:
        console.print_json(data=projection)
        return
    _print_review(projection)


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
                "database_path": result.database_path,
                "config_path": result.config_path,
                "workspace_path": result.workspace_path,
                "curriculum": result.curriculum,
                "recommendation": result.recommendation,
                "assignment": result.assignment,
                "journey": result.journey,
                "validation_checks": result.validation_checks,
                "automated_evidence": result.automated_evidence,
                "qualitative_evaluation": result.qualitative_evaluation,
                "status": result.status,
                "report": result.report.data,
            }
        )
        return
    passed = sum(bool(item["automated_passed"]) for item in result.journey)
    failed = len(result.journey) - passed
    stages = Table.grid(padding=(0, 2))
    stages.add_column(width=3, style="green bold")
    stages.add_column(style="bold")
    stages.add_column(style="dim")
    stages.add_row("1", "Curriculum loaded", result.curriculum)
    stages.add_row("2", "Learning history", f"{len(result.journey)} evaluated submissions")
    stages.add_row(
        "3",
        "Deterministic evidence",
        f"{passed} passing · {failed} failing submissions",
    )
    stages.add_row(
        "4",
        "Adaptive history",
        "confident failures · challenge · transfer · recurrence",
    )
    stages.add_row("5", "Learner model", "transactional evidence and spaced reviews")
    stages.add_row(
        "6",
        "Next assignment",
        f"{result.assignment['id']} · difficulty {result.assignment['difficulty']}",
    )
    stages.add_row("7", "Progress report", "weekly Markdown + structured data")
    console.print(
        Panel(
            stages,
            title="[bold]Adaptive Tutor · local demo[/bold]",
            border_style="green",
        )
    )
    console.print(
        f"[bold]Why this is next:[/bold] {result.recommendation['reason']}\n"
        "[dim]No GitHub credentials, private curriculum, network calls, or live model "
        "were used.[/dim]"
    )
    if keep and result.config_path:
        console.print(
            f"\nDemo state: [bold]{keep}[/bold]\n"
            f"Inspect it: [bold]adaptive-tutor --config {result.config_path} status[/bold]"
        )


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
def grader(
    socket: Path = typer.Option(
        Path("/run/adaptive-tutor-grader/grader.sock"),
        help="Owner-only Unix socket shared with the durable worker.",
    ),
    command: str = typer.Option("codex", help="Codex CLI executable."),
    model: str | None = typer.Option(None, help="Optional explicit grading model."),
    timeout_seconds: int = typer.Option(600, min=30, max=3600),
    socket_group: str | None = typer.Option(
        None,
        help="Optional group allowed to connect to the grader socket.",
    ),
) -> None:
    """Run the credential-bearing grader without tutor state or GitHub access."""
    socket_path = socket.expanduser().absolute()
    group_id = _resolve_socket_group(socket_group)
    _prepare_socket_directory(socket_path.parent, group_id)
    if socket_path.is_symlink():
        _abort(f"Refusing to replace symlinked grader path: {socket_path}")
    if socket_path.exists():
        if not socket_path.is_socket():
            _abort(f"Refusing to replace non-socket grader path: {socket_path}")
        socket_path.unlink()
    settings = CodexSettings(
        command=command,
        model=model,
        timeout_seconds=timeout_seconds,
        enabled=True,
        sandbox="read-only",
    )
    socket_mode = 0o660 if group_id is not None else 0o600
    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    socket_identity: tuple[int, int] | None = None
    old_umask = os.umask(0o077)
    try:
        try:
            listener.bind(str(socket_path))
            listener.listen(socket_module.SOMAXCONN)
            if group_id is not None:
                os.chown(socket_path, -1, group_id)
            socket_path.chmod(socket_mode)
            socket_info = socket_path.lstat()
            expected_group = group_id if group_id is not None else os.getegid()
            if (
                not stat.S_ISSOCK(socket_info.st_mode)
                or socket_info.st_uid != os.geteuid()
                or socket_info.st_gid != expected_group
                or stat.S_IMODE(socket_info.st_mode) != socket_mode
            ):
                _abort("Grader socket ownership or mode verification failed")
            socket_identity = (socket_info.st_dev, socket_info.st_ino)
        except OSError as exc:
            _abort(f"Could not create protected grader socket: {exc}")
        _run_grader_server(create_grader_app(settings), listener)
    finally:
        listener.close()
        os.umask(old_umask)
        if socket_identity is not None:
            try:
                current = socket_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (
                    stat.S_ISSOCK(current.st_mode)
                    and (current.st_dev, current.st_ino) == socket_identity
                ):
                    socket_path.unlink()


def _resolve_socket_group(group_name: str | None) -> int | None:
    if group_name is None:
        return None
    if os.name != "posix":
        _abort("Group-scoped grader sockets require a POSIX host")
    import grp

    try:
        group_id = grp.getgrnam(group_name).gr_gid
    except KeyError:
        _abort(f"Grader socket group does not exist: {group_name}")
    if group_id not in {os.getgid(), os.getegid(), *os.getgroups()}:
        _abort(f"Grader process is not a member of socket group: {group_name}")
    return group_id


def _prepare_socket_directory(directory: Path, group_id: int | None) -> None:
    mode = 0o750 if group_id is not None else 0o700
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=mode)
        if directory.resolve(strict=True) != directory:
            _abort(f"Grader socket directory must not contain symlinks: {directory}")
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            _abort(f"Grader socket directory must be owned by the grader: {directory}")
        if group_id is not None:
            os.chown(directory, -1, group_id)
        directory.chmod(mode)
        verified = directory.lstat()
        expected_group = group_id if group_id is not None else verified.st_gid
        if verified.st_gid != expected_group or stat.S_IMODE(verified.st_mode) != mode:
            _abort("Grader socket directory ownership or mode verification failed")
    except OSError as exc:
        _abort(f"Could not prepare protected grader socket directory: {exc}")


def _run_grader_server(application: Any, listener: socket_module.socket) -> None:
    config = uvicorn.Config(
        application,
        log_level="info",
        access_log=False,
    )
    uvicorn.Server(config).run(sockets=[listener])


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


@app.command("evaluate-public", hidden=True)
def evaluate_public_command(
    verification_key: Path = typer.Option(
        ..., exists=True, dir_okay=False, help="Protected evaluator verification key."
    ),
    workspace: Path = typer.Option(
        ..., exists=True, file_okay=False, help="Untrusted learner checkout."
    ),
    output: Path = typer.Option(
        ..., dir_okay=False, help="Evidence path outside the learner checkout."
    ),
    assignment_id: str = typer.Option(..., help="Assignment identifier."),
    branch: str = typer.Option(..., help="Assignment branch."),
    commit_sha: str = typer.Option(..., help="Exact evaluated learner commit."),
    dispatch_nonce: str = typer.Option(..., help="One-attempt dispatch nonce."),
    manifest_digest: str = typer.Option(..., help="Expected signed manifest digest."),
    evaluator_kit_digest: str = typer.Option(..., help="Expected evaluator-kit digest."),
    evaluator_ref: str = typer.Option(..., help="Exact Adaptive Tutor source commit."),
    workflow_digest: str = typer.Option(..., help="Protected workflow content digest."),
    workflow_commit: str = typer.Option(..., help="Protected workflow commit."),
    repository_id: int = typer.Option(..., min=1, help="Immutable GitHub repository ID."),
) -> None:
    """Run the signed public evaluator on a GitHub-hosted runner."""
    try:
        evidence = evaluate_public_workspace_to_file(
            verification_key_path=verification_key,
            workspace=workspace,
            output_path=output,
            assignment_id=assignment_id,
            branch=branch,
            commit_sha=commit_sha,
            dispatch_nonce=dispatch_nonce,
            expected_manifest_digest=manifest_digest,
            expected_evaluator_kit_digest=evaluator_kit_digest,
            evaluator_ref=evaluator_ref,
            workflow_digest=workflow_digest,
            workflow_commit=workflow_commit,
            repository_id=repository_id,
        )
    except (TutorError, ValueError, OSError) as exc:
        _abort(str(exc))
    state = "passed" if evidence.learner_passed else "failed"
    console.print(f"Public deterministic evaluation {state}; evidence written to {output}")


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


def _parse_goal_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _abort("--target-date must use YYYY-MM-DD.")
    if parsed.isoformat() != value:
        _abort("--target-date must use YYYY-MM-DD.")
    return parsed


def _print_goal(goal: LearningGoal) -> None:
    console.print(
        Text(
            f"LEARNING GOAL · revision {goal.revision} · {goal.status.value}",
            style="bold green",
        )
    )
    console.print(Text(goal.statement, style="bold"))
    console.print(f"Target: {goal.target_date.isoformat() if goal.target_date else 'none'}")
    console.print(f"Profile: {goal.profile_id}")
    domains = ", ".join(goal.focus_domains) or "none"
    concepts = ", ".join(goal.focus_concepts) or "none"
    console.print(f"Focus domains: {domains}")
    console.print(f"Focus concepts: {concepts}")


def _concept_names(database: Database) -> dict[str, str]:
    return {
        str(item["id"]): str(item["name"])
        for item in database.fetch_all("SELECT id, name FROM concepts")
    }


def _assignment_location(
    settings: TutorSettings,
    database: Database,
    assignment: dict[str, Any],
) -> tuple[str, str]:
    if assignment.get("publication_error"):
        return "State", "GitHub publication is paused"
    if assignment.get("pull_number") and settings.github.owner:
        return (
            "Open",
            f"https://github.com/{settings.github.owner}/{settings.github.workspace_repo}/"
            f"pull/{assignment['pull_number']}",
        )
    demo_workspace = database.fetch_one(
        "SELECT value_json FROM configuration WHERE key='demo_workspace_path'"
    )
    if demo_workspace:
        try:
            value = json.loads(str(demo_workspace["value_json"]))
            if isinstance(value, str) and value:
                return "Workspace", value
        except json.JSONDecodeError:
            pass
    return "Branch", str(assignment.get("branch_name") or "not published")


def _print_readiness(domains: list[Any], *, verbose: bool = False) -> None:
    values = [item.model_dump() if hasattr(item, "model_dump") else item for item in domains]
    assessed_count = sum(int(item["assessed_concept_count"]) for item in values)
    concept_count = sum(int(item["concept_count"]) for item in values)
    evidence_count = sum(int(item["evidence_count"]) for item in values)
    console.print("[bold]Readiness[/bold]")
    if assessed_count < concept_count:
        console.print(
            f"Evidence covers [bold]{assessed_count}/{concept_count} concepts[/bold] "
            f"({evidence_count} observations). Readiness is provisional."
        )
    else:
        console.print(
            f"Evidence covers all {concept_count} concepts across {evidence_count} observations."
        )
    assessed = sorted(
        (item for item in values if item["readiness"] is not None),
        key=lambda item: float(item["readiness"]),
    )
    if assessed:
        table = Table("Domain", "Readiness", "Confidence", "Coverage", box=box.SIMPLE)
        for item in assessed:
            readiness_value = float(item["readiness"])
            uncertainty = float(item["uncertainty"])
            color = (
                "green" if readiness_value >= 0.7 else "yellow" if readiness_value >= 0.4 else "red"
            )
            confidence = "high" if uncertainty <= 0.3 else "medium" if uncertainty <= 0.6 else "low"
            table.add_row(
                str(item["domain"]).replace("-", " ").title(),
                f"[{color}]{readiness_value:.0%}[/{color}]",
                confidence,
                f"{item['assessed_concept_count']}/{item['concept_count']}",
            )
        console.print(table)
    unassessed = [item for item in values if item["readiness"] is None]
    if unassessed:
        names = ", ".join(str(item["domain"]).replace("-", " ") for item in unassessed)
        label = "Unassessed domains" if len(unassessed) > 1 else "Unassessed domain"
        console.print(f"[dim]{label}: {names}[/dim]")
    if verbose and values:
        console.print(
            "[dim]Confidence describes uncertainty in the learner estimate, "
            "not confidence reported by the learner.[/dim]"
        )


def _print_review(projection: dict[str, Any]) -> None:
    assignment = projection["assignment"]
    result = projection["review"]
    console.print(Text(f"REVIEW · {assignment['id']}", style="bold green"))
    console.print(Text(str(assignment["title"]), style="bold"))
    classification = str(result["classification"]).replace("_", " ")
    console.print(
        f"[bold]{float(result['overall_score']):.0f}/100[/bold] · {classification.title()} · "
        f"{str(result['review_kind']).title()} review · "
        f"grader confidence {float(result['grader_confidence']):.0%}"
    )
    console.print()
    console.print("[bold]Feedback[/bold]")
    console.print(Text(str(result["feedback_summary"])))
    for detail in result["feedback_details"]:
        console.print(Text(f"- {detail}"))

    console.print()
    dimensions = Table("Dimension", "Score", "Rationale", box=box.SIMPLE)
    for dimension in result["dimensions"]:
        dimensions.add_row(
            str(dimension["dimension"]).replace("_", " ").title(),
            f"{float(dimension['score']):.0f}",
            Text(str(dimension["rationale"])),
        )
    console.print(dimensions)

    console.print("[bold]Follow-up[/bold]")
    console.print(
        Text(f"{str(result['follow_up']).replace('_', ' ').title()}: {result['follow_up_reason']}")
    )
    console.print()
    console.print("[bold]Attempts[/bold]")
    attempts = Table("Stage", "Submitted", "Outcome", "Confidence", "Score", "Commit")
    for attempt in projection["attempts"]:
        attempt_reviews = attempt["reviews"]
        score = f"{float(attempt_reviews[0]['overall_score']):.0f}" if attempt_reviews else "—"
        confidence = (
            f"{int(attempt['learner_confidence'])}%"
            if attempt["learner_confidence"] is not None
            else "—"
        )
        attempts.add_row(
            str(attempt["stage_number"]),
            str(attempt["submitted_at"])[:10],
            str(attempt["outcome"] or "pending").title(),
            confidence,
            score,
            str(attempt["commit_sha"])[:8],
        )
    console.print(attempts)
    if projection["pr_url"]:
        console.print(f"[green]Pull request:[/green] {projection['pr_url']}")
    else:
        console.print("[dim]Pull request: not available[/dim]")


def _print_console_report(document: ReportDocument, *, verbose: bool) -> None:
    data = document.data
    activity = data["study_activity"]
    console.print(
        f"[bold]{document.period_type.title()} review[/bold]  "
        f"[dim]{document.period_start[:10]} to {document.period_end[:10]}[/dim]"
    )
    console.print(
        f"{activity['attempts']} attempts · {activity['assignments']} assignments · "
        f"{activity['planned_minutes']} planned minutes"
    )
    console.print("\n[green bold]IMPROVED[/green bold]")
    positive = [item for item in data["mastery_movement"] if float(item["movement"]) > 0]
    if positive:
        for item in positive[:3]:
            console.print(
                f"- {item['name']}: [green]+{float(item['movement']):.0%}[/green] "
                f"from {item['evidence_count']} observation(s)"
            )
    else:
        console.print("- No positive mastery movement was recorded in this period.")
    console.print("\n[yellow bold]NEEDS ATTENTION[/yellow bold]")
    weaknesses = data["weaknesses"]
    if weaknesses:
        for item in weaknesses[:3]:
            console.print(f"- {item['name']}: {float(item['mastery']):.0%} mastery")
    else:
        console.print("- More evidence is needed before naming a weakness.")
    retention = data["retention"]
    if retention["due_reviews"]:
        console.print(f"- {retention['due_reviews']} retrieval review(s) are due")
    if data["active_misconceptions"]:
        console.print(f"- {data['active_misconceptions']} misconception(s) remain open")
    console.print("\n[green bold]NEXT SESSION[/green bold]")
    if data["recommended_focus"]:
        console.print(f"Focus on {', '.join(data['recommended_focus'][:3])}.")
    else:
        console.print("Build baseline evidence for an unassessed concept.")
    if verbose:
        calibration = data["confidence_calibration"]
        usage = data["model_usage"]
        console.print(
            "\n[dim]Confidence observations: "
            f"{calibration['observations']} · calibration error: "
            f"{float(calibration['mean_absolute_error']):.1%} · model calls: "
            f"{usage['invocations']} · recorded cost: ${float(usage['cost_usd']):.4f}[/dim]"
        )


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
