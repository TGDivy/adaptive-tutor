from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adaptive_tutor.cli import app


def test_cli_initialization_and_local_read_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ADAPTIVE_TUTOR_API_TOKEN", "ADAPTIVE_TUTOR_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    runner = CliRunner()
    config_path = tmp_path / "config" / "config.yaml"
    data_dir = tmp_path / "state"
    prefix = ["--config", str(config_path)]

    initialized = runner.invoke(
        app,
        [*prefix, "init", "--data-dir", str(data_dir)],
    )
    assert initialized.exit_code == 0, initialized.output
    assert "Adaptive Tutor initialized" in initialized.output

    status = runner.invoke(app, [*prefix, "status", "--json"])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["active_curriculum"] == "systems-foundations"

    recommendation = runner.invoke(
        app,
        [*prefix, "next", "--dry-run", "--available-minutes", "25", "--json"],
    )
    assert recommendation.exit_code == 0, recommendation.output
    candidate = json.loads(recommendation.output)[0]
    assert candidate["concept_id"]
    assert 1 <= candidate["target_difficulty"] <= 10

    paused = runner.invoke(app, [*prefix, "pause"])
    assert paused.exit_code == 0, paused.output
    paused_status = runner.invoke(app, [*prefix, "status", "--json"])
    assert json.loads(paused_status.output)["paused"] is True
    assert runner.invoke(app, [*prefix, "resume"]).exit_code == 0

    output = tmp_path / "weekly.json"
    report = runner.invoke(
        app,
        [*prefix, "report", "--format", "json", "--output", str(output)],
    )
    assert report.exit_code == 0, report.output
    assert json.loads(output.read_text(encoding="utf-8"))["study_activity"][
        "assignments"
    ] == 0


def test_cli_help_lists_product_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "init",
        "doctor",
        "status",
        "next",
        "current",
        "hint",
        "readiness",
        "report",
        "history",
        "concepts",
        "pause",
        "resume",
        "demo",
    ):
        assert command in result.output
