from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from adaptive_tutor.cli import app
from adaptive_tutor.db import Database
from adaptive_tutor.errors import ConfigurationError
from adaptive_tutor.setup import SETUP_STEPS, SetupRun, SetupService, StepOutcome


class RecordingExecutor:
    def __init__(self, outcomes: dict[str, StepOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    def execute(self, step: str, run: SetupRun) -> StepOutcome:
        self.calls.append(step)
        return self.outcomes.get(step, StepOutcome.complete(f"{step} complete"))


def test_setup_state_is_durable_resumable_and_ordered(
    initialized: tuple[Database, object], tmp_path: Path
) -> None:
    database, _ = initialized
    service = SetupService(database)
    created = service.begin(
        public_url="https://tutor.example.test/",
        goal_statement="Build reliable network services.",
        config_path=tmp_path / "config.yaml",
        learner_id="learner",
        curriculum_id="systems-foundations",
    )
    assert created.status == "provisioning"
    assert [step.name for step in created.steps] == list(SETUP_STEPS)

    waiting = RecordingExecutor(
        {
            "github_app": StepOutcome.wait(
                "Browser approval is required", action="Open the setup approval URL"
            )
        }
    )
    paused = service.resume(waiting)
    assert paused.status == "action_required"
    assert waiting.calls == list(SETUP_STEPS[:5])
    assert paused.steps[4].status == "waiting_user"
    assert paused.steps[4].attempts == 1
    assert paused.steps[5].status == "pending"

    resumed_executor = RecordingExecutor()
    completed = SetupService(database).resume(resumed_executor)
    assert completed.status == "ready"
    assert resumed_executor.calls == list(SETUP_STEPS[4:])
    assert all(step.status == "complete" for step in completed.steps)
    assert completed.completed_at is not None


def test_setup_rejects_changed_inputs_and_secret_external_ids(
    initialized: tuple[Database, object], tmp_path: Path
) -> None:
    database, _ = initialized
    service = SetupService(database)
    service.begin(
        public_url="https://tutor.example.test",
        goal_statement="Learn systems reasoning.",
        config_path=tmp_path / "config.yaml",
        learner_id="learner",
        curriculum_id="systems-foundations",
    )
    with pytest.raises(ConfigurationError, match="unfinished setup"):
        service.begin(
            public_url="https://other.example.test",
            goal_statement="Different goal.",
            config_path=tmp_path / "config.yaml",
            learner_id="learner",
            curriculum_id="systems-foundations",
        )

    executor = RecordingExecutor(
        {
            "configuration": StepOutcome.complete(
                "unsafe", external_ids={"value": "webhook_secret=do-not-store"}
            )
        }
    )
    with pytest.raises(ValueError, match="not safe to persist"):
        service.resume(executor)


def test_cli_setup_persists_goal_and_reports_tls_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> object:
        raise httpx.ConnectError("not listening")

    monkeypatch.setattr("adaptive_tutor.setup.httpx.get", unavailable)
    runner = CliRunner()
    config = tmp_path / "config.yaml"
    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "setup",
            "--public-url",
            "https://tutor.example.test",
            "--goal",
            "Build reliable network services.",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "action_required"
    assert payload["steps"][0]["status"] == "complete"
    assert payload["steps"][1]["status"] == "complete"
    assert payload["steps"][2]["status"] == "waiting_user"

    status = runner.invoke(app, ["--config", str(config), "setup", "status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["id"] == payload["id"]

    database = Database(tmp_path / "state" / "tutor.sqlite3")
    goal = database.fetch_one("SELECT statement, status FROM learning_goals")
    assert goal == {"statement": "Build reliable network services.", "status": "active"}
