"""Durable, resumable server and private GitHub setup orchestration."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import httpx
from pydantic import Field, field_validator

from .codex import CodexRunner
from .config import TutorSettings
from .db import Database
from .errors import ConfigurationError, TutorError
from .goals import GoalService
from .models import StrictModel
from .security import redact
from .time import iso_now

SetupRunStatus = Literal["provisioning", "action_required", "failed", "ready"]
SetupStepStatus = Literal[
    "pending",
    "running",
    "waiting_user",
    "failed_retryable",
    "failed_terminal",
    "complete",
]

SETUP_STEPS = (
    "configuration",
    "learning_goal",
    "service_tls",
    "github_repository",
    "github_app",
    "evaluator_controls",
    "webhook_round_trip",
    "codex_canary",
    "hosted_ci_probe",
    "first_assignment",
)


class SetupStep(StrictModel):
    name: str
    position: int = Field(ge=1)
    status: SetupStepStatus
    attempts: int = Field(ge=0)
    detail: str = ""
    action: str | None = None
    external_ids: dict[str, str | int | bool] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str


class SetupRun(StrictModel):
    id: str
    status: SetupRunStatus
    public_url: str
    goal_statement: str
    config_path: str
    learner_id: str
    curriculum_id: str
    goal_id: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    steps: list[SetupStep] = Field(default_factory=list)

    @field_validator("public_url")
    @classmethod
    def public_https_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith("https://"):
            raise ValueError("setup public_url must use HTTPS")
        return normalized


@dataclass(frozen=True)
class StepOutcome:
    status: Literal["complete", "waiting_user", "failed_retryable", "failed_terminal"]
    detail: str
    action: str | None = None
    external_ids: dict[str, str | int | bool] = field(default_factory=dict)

    @classmethod
    def complete(
        cls,
        detail: str,
        *,
        external_ids: dict[str, str | int | bool] | None = None,
    ) -> StepOutcome:
        return cls("complete", detail, external_ids=external_ids or {})

    @classmethod
    def wait(cls, detail: str, *, action: str) -> StepOutcome:
        return cls("waiting_user", detail, action=action)


class SetupExecutor(Protocol):
    def execute(self, step: str, run: SetupRun) -> StepOutcome: ...


class SetupService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def begin(
        self,
        *,
        public_url: str,
        goal_statement: str,
        config_path: Path,
        learner_id: str,
        curriculum_id: str,
    ) -> SetupRun:
        normalized_url = public_url.rstrip("/")
        if not normalized_url.startswith("https://"):
            raise ValueError("--public-url must use HTTPS")
        goal = goal_statement.strip()
        if not goal or len(goal) > 2000:
            raise ValueError("--goal must contain 1 to 2000 characters")
        existing = self.current()
        if existing and existing.status != "ready":
            if existing.public_url != normalized_url or existing.goal_statement != goal:
                raise ConfigurationError(
                    "An unfinished setup already exists; resume it before changing URL or goal"
                )
            return existing
        run_id = str(uuid.uuid4())
        now = iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO setup_runs(
                    id, status, public_url, goal_statement, config_path,
                    learner_id, curriculum_id, created_at, updated_at
                ) VALUES (?, 'provisioning', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    normalized_url,
                    goal,
                    str(config_path.expanduser().resolve()),
                    learner_id,
                    curriculum_id,
                    now,
                    now,
                ),
            )
            for position, name in enumerate(SETUP_STEPS, 1):
                connection.execute(
                    """
                    INSERT INTO setup_steps(run_id, name, position, status, updated_at)
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (run_id, name, position, now),
                )
        run = self.get(run_id)
        if run is None:  # pragma: no cover - transaction invariant
            raise RuntimeError("Setup run was not persisted")
        return run

    def current(self) -> SetupRun | None:
        row = self.database.fetch_one(
            "SELECT id FROM setup_runs ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        return self.get(str(row["id"])) if row else None

    def get(self, run_id: str) -> SetupRun | None:
        row = self.database.fetch_one("SELECT * FROM setup_runs WHERE id=?", (run_id,))
        if row is None:
            return None
        steps = self.database.fetch_all(
            "SELECT * FROM setup_steps WHERE run_id=? ORDER BY position", (run_id,)
        )
        step_payloads: list[dict[str, Any]] = []
        for step in steps:
            payload = dict(step)
            payload["external_ids"] = json.loads(payload.pop("external_ids_json"))
            payload.pop("run_id", None)
            step_payloads.append(payload)
        return SetupRun.model_validate(
            {
                **row,
                "steps": step_payloads,
            }
        )

    def resume(self, executor: SetupExecutor) -> SetupRun:
        run = self.current()
        if run is None:
            raise ConfigurationError("No setup exists; run adaptive-tutor setup first")
        if run.status == "ready":
            return run
        self.database.execute(
            """
            UPDATE setup_steps SET status='failed_retryable',
                detail='Interrupted setup step will be retried', updated_at=?
            WHERE run_id=? AND status='running'
            """,
            (iso_now(), run.id),
        )
        self._set_run_status(run.id, "provisioning")
        run = self.get(run.id)
        if run is None:  # pragma: no cover - row cannot disappear
            raise RuntimeError("Setup run disappeared")
        for step in run.steps:
            if step.status == "complete":
                continue
            self._mark_running(run.id, step.name)
            current = self.get(run.id)
            if current is None:  # pragma: no cover
                raise RuntimeError("Setup run disappeared")
            try:
                outcome = executor.execute(step.name, current)
            except (OSError, ValueError, TutorError, httpx.HTTPError) as exc:
                outcome = StepOutcome("failed_retryable", redact(str(exc))[:1000])
            self._apply_outcome(run.id, step.name, outcome)
            if outcome.status != "complete":
                status: SetupRunStatus = (
                    "action_required" if outcome.status == "waiting_user" else "failed"
                )
                self._set_run_status(run.id, status)
                result = self.get(run.id)
                if result is None:  # pragma: no cover
                    raise RuntimeError("Setup run disappeared")
                return result
        now = iso_now()
        self.database.execute(
            """
            UPDATE setup_runs SET status='ready', completed_at=?, updated_at=? WHERE id=?
            """,
            (now, now, run.id),
        )
        result = self.get(run.id)
        if result is None:  # pragma: no cover
            raise RuntimeError("Setup run disappeared")
        return result

    def set_goal_id(self, run_id: str, goal_id: str) -> None:
        self.database.execute(
            "UPDATE setup_runs SET goal_id=?, updated_at=? WHERE id=?",
            (goal_id, iso_now(), run_id),
        )

    def _mark_running(self, run_id: str, name: str) -> None:
        now = iso_now()
        self.database.execute(
            """
            UPDATE setup_steps SET status='running', attempts=attempts+1,
                started_at=COALESCE(started_at, ?), detail='', action=NULL, updated_at=?
            WHERE run_id=? AND name=?
            """,
            (now, now, run_id, name),
        )

    def _apply_outcome(self, run_id: str, name: str, outcome: StepOutcome) -> None:
        detail = redact(outcome.detail).strip()[:1000]
        action = redact(outcome.action).strip()[:1000] if outcome.action else None
        external = _validated_external_ids(outcome.external_ids)
        now = iso_now()
        self.database.execute(
            """
            UPDATE setup_steps SET status=?, detail=?, action=?, external_ids_json=?,
                completed_at=?, updated_at=? WHERE run_id=? AND name=?
            """,
            (
                outcome.status,
                detail,
                action,
                json.dumps(external, sort_keys=True),
                now if outcome.status == "complete" else None,
                now,
                run_id,
                name,
            ),
        )

    def _set_run_status(self, run_id: str, status: SetupRunStatus) -> None:
        self.database.execute(
            "UPDATE setup_runs SET status=?, updated_at=? WHERE id=?",
            (status, iso_now(), run_id),
        )


class LiveSetupExecutor:
    """Execute setup checks that can be proven from the current host and state."""

    def __init__(
        self,
        settings: TutorSettings,
        database: Database,
        *,
        config_path: Path,
    ) -> None:
        self.settings = settings
        self.database = database
        self.config_path = config_path.expanduser().resolve()
        self.service = SetupService(database)

    def execute(self, step: str, run: SetupRun) -> StepOutcome:
        handler = cast(Callable[[SetupRun], StepOutcome], getattr(self, f"_{step}"))
        return handler(run)

    def _configuration(self, run: SetupRun) -> StepOutcome:
        if not self.config_path.is_file():
            return StepOutcome("failed_terminal", "Adaptive Tutor configuration is missing")
        if self.settings.github.webhook_url != run.public_url:
            return StepOutcome.wait(
                "The configured public URL does not match this setup run",
                action=f"Set github.webhook_url to {run.public_url}",
            )
        return StepOutcome.complete("Secure configuration and SQLite migrations are present")

    def _learning_goal(self, run: SetupRun) -> StepOutcome:
        goal = GoalService(self.database).set(
            run.learner_id,
            run.curriculum_id,
            self.settings.active_profile,
            run.goal_statement,
        )
        self.service.set_goal_id(run.id, goal.id)
        return StepOutcome.complete(
            "Learning goal is durable and active", external_ids={"goal_revision": goal.revision}
        )

    def _service_tls(self, run: SetupRun) -> StepOutcome:
        try:
            response = httpx.get(run.public_url + "/readyz", timeout=5, follow_redirects=False)
        except httpx.HTTPError as exc:
            return StepOutcome.wait(
                "The public HTTPS readiness endpoint is not reachable",
                action=f"Start the tutor behind valid TLS at {run.public_url}: {redact(str(exc))}",
            )
        if response.status_code != 200:
            return StepOutcome(
                "failed_retryable",
                f"Public readiness returned HTTP {response.status_code}",
            )
        return StepOutcome.complete("Public HTTPS readiness is healthy")

    def _github_repository(self, run: SetupRun) -> StepOutcome:
        gh = shutil.which("gh")
        if not gh:
            return StepOutcome.wait(
                "GitHub CLI is required for repository bootstrap",
                action="Install gh, run gh auth login, then adaptive-tutor setup resume",
            )
        if not self.settings.github.owner:
            return StepOutcome.wait(
                "GitHub owner is not configured",
                action="Run gh auth login; repository creation will resume automatically",
            )
        full_name = f"{self.settings.github.owner}/{self.settings.github.workspace_repo}"
        viewed = subprocess.run(  # noqa: S603 - fixed gh command and configured identifier
            [gh, "repo", "view", full_name, "--json", "id,isPrivate,nameWithOwner"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if viewed.returncode:
            return StepOutcome.wait(
                "The configured private workspace does not exist or is inaccessible",
                action=(
                    f"Create {full_name} as a private repository or correct github.owner and "
                    "github.workspace_repo, then resume"
                ),
            )
        payload = json.loads(viewed.stdout)
        if payload.get("isPrivate") is not True:
            return StepOutcome("failed_terminal", "The learning workspace is not private")
        return StepOutcome.complete(
            "Private GitHub workspace is available",
            external_ids={"repository_id": str(payload["id"])},
        )

    def _github_app(self, run: SetupRun) -> StepOutcome:
        key = self.settings.github.private_key_path
        if (
            self.settings.github.app_id
            and self.settings.github.installation_id
            and key
            and key.is_file()
            and self.settings.webhook_secret
        ):
            return StepOutcome.complete(
                "Least-privilege GitHub App installation credentials are present",
                external_ids={
                    "app_id": self.settings.github.app_id,
                    "installation_id": self.settings.github.installation_id,
                },
            )
        return StepOutcome.wait(
            "GitHub App creation or installation approval is required",
            action=f"Open {run.public_url}/setup/github-app and follow the GitHub approval",
        )

    def _evaluator_controls(self, run: SetupRun) -> StepOutcome:
        row = self.database.fetch_one(
            "SELECT repository_id, evaluator_key_id FROM evaluator_control_planes LIMIT 1"
        )
        if row is None:
            return StepOutcome.wait(
                "Protected evaluator workflow and signing key are not provisioned",
                action="Complete GitHub App installation, then run adaptive-tutor setup resume",
            )
        return StepOutcome.complete(
            "Protected evaluator controls are recorded",
            external_ids={
                "repository_id": int(row["repository_id"]),
                "evaluator_key_id": str(row["evaluator_key_id"]),
            },
        )

    def _webhook_round_trip(self, run: SetupRun) -> StepOutcome:
        row = self.database.fetch_one(
            """
            SELECT id FROM events
            WHERE source='github' AND event_type IN ('ping', 'installation')
              AND received_at >= ? ORDER BY received_at DESC LIMIT 1
            """,
            (run.created_at,),
        )
        if row is None:
            return StepOutcome.wait(
                "No signed GitHub webhook has reached this setup run",
                action="Finish the GitHub App install and redeliver its ping, then resume",
            )
        return StepOutcome.complete(
            "A signed GitHub webhook reached durable event storage",
            external_ids={"event_id": str(row["id"])},
        )

    def _codex_canary(self, run: SetupRun) -> StepOutcome:
        previous = self.database.fetch_one(
            """
            SELECT id FROM model_invocations
            WHERE purpose='setup_canary' AND status='succeeded' AND started_at >= ?
            ORDER BY started_at DESC LIMIT 1
            """,
            (run.created_at,),
        )
        if previous:
            return StepOutcome.complete(
                "Isolated Codex canary produced schema-valid output",
                external_ids={"invocation_id": str(previous["id"])},
            )
        if not self.settings.codex.enabled or self.settings.codex.socket_path is None:
            return StepOutcome.wait(
                "The isolated Codex grader is not enabled",
                action="Authenticate Codex, start the grader service, enable codex, then resume",
            )
        evaluation = CodexRunner(self.settings.codex, self.database).grade(
            _CODEX_CANARY_PROMPT,
            prompt_version="setup-canary-v1",
            purpose="setup_canary",
        )
        row = self.database.fetch_one(
            """
            SELECT id FROM model_invocations
            WHERE purpose='setup_canary' AND status='succeeded'
            ORDER BY started_at DESC LIMIT 1
            """
        )
        return StepOutcome.complete(
            f"Isolated Codex canary passed with {evaluation.grader_confidence:.0%} confidence",
            external_ids={"invocation_id": str(row["id"]) if row else "recorded"},
        )

    def _hosted_ci_probe(self, run: SetupRun) -> StepOutcome:
        row = self.database.fetch_one(
            "SELECT value_json FROM configuration WHERE key='setup_hosted_ci_probe'"
        )
        if row and json.loads(str(row["value_json"])).get("passed") is True:
            return StepOutcome.complete("Credential-free GitHub-hosted CI probe passed")
        return StepOutcome.wait(
            "A credential-free GitHub-hosted CI probe has not passed",
            action="Resume after evaluator controls are installed to dispatch the setup probe",
        )

    def _first_assignment(self, run: SetupRun) -> StepOutcome:
        row = self.database.fetch_one(
            """
            SELECT id, pull_number FROM assignments
            WHERE learner_id=? AND pull_number IS NOT NULL
            ORDER BY created_at LIMIT 1
            """,
            (run.learner_id,),
        )
        if row is None:
            return StepOutcome.wait(
                "The first live assignment pull request has not been published",
                action="Run adaptive-tutor next after the hosted CI probe passes",
            )
        return StepOutcome.complete(
            "The first live assignment pull request is available",
            external_ids={"assignment_id": str(row["id"]), "pull_number": int(row["pull_number"])},
        )


def _validated_external_ids(
    values: dict[str, str | int | bool],
) -> dict[str, str | int | bool]:
    result: dict[str, str | int | bool] = {}
    for name, value in values.items():
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
            raise ValueError("Setup external identifier name is invalid")
        if isinstance(value, str) and (
            len(value) > 200 or re.search(r"token|secret|private[-_ ]?key", value, re.I)
        ):
            raise ValueError("Setup external identifier value is not safe to persist")
        result[name] = value
    return result


_CODEX_CANARY_PROMPT = """You are performing an Adaptive Tutor installation canary.
Return only a JSON object matching the supplied schema. Use exactly three dimensions:
correctness, reasoning, and communication, all scored 100 with short rationales. Set
overall_score to 100, grader_confidence to 1, concept_evidence and misconceptions to empty
arrays, feedback_summary to 'The isolated structured-output canary passed.', feedback_details
to one short item, classification to 'correct', follow_up to 'none', follow_up_reason to
'Installation canary only.', and escalation_recommended to false. Do not inspect files.
"""
