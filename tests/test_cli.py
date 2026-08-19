from __future__ import annotations

import grp
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from adaptive_tutor.cli import app
from adaptive_tutor.curriculum import bundled_curriculum_path


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


def test_cli_fresh_install_empty_states_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ADAPTIVE_TUTOR_API_TOKEN", "ADAPTIVE_TUTOR_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    runner = CliRunner()
    missing = runner.invoke(
        app, ["--config", str(tmp_path / "missing.yaml"), "status"]
    )
    assert missing.exit_code == 1
    assert "Adaptive Tutor error" in missing.output

    config_path = tmp_path / "config.yaml"
    prefix = ["--config", str(config_path)]
    initialized = runner.invoke(
        app,
        [
            *prefix,
            "init",
            "--data-dir",
            str(tmp_path / "state"),
            "--github-owner",
            "example-owner",
        ],
    )
    assert initialized.exit_code == 0, initialized.output
    assert "configure a GitHub App" in initialized.output

    duplicate = runner.invoke(app, [*prefix, "init"])
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.output

    status = runner.invoke(app, [*prefix, "status", "--verbose"])
    assert status.exit_code == 0, status.output
    assert "NEXT RECOMMENDATION" in status.output
    assert "Model cost" in status.output

    current = runner.invoke(app, [*prefix, "current"])
    assert current.exit_code == 0
    assert "No active assignment" in current.output
    no_hint = runner.invoke(app, [*prefix, "hint"])
    assert no_hint.exit_code == 1
    assert "No active assignment" in no_hint.output

    report = runner.invoke(app, [*prefix, "report", "--verbose"])
    assert report.exit_code == 0, report.output
    assert "More evidence is needed" in report.output
    assert "Confidence observations" in report.output

    missing_webhook = runner.invoke(app, [*prefix, "webhook-setup"])
    assert missing_webhook.exit_code == 1
    assert "webhook secret" in missing_webhook.output

    version = runner.invoke(app, ["--version"])
    assert version.exit_code == 0
    assert "adaptive-tutor" in version.output


def test_cli_kept_demo_exercises_daily_and_operational_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    demo_root = tmp_path / "demo"
    demo = runner.invoke(app, ["demo", "--keep", str(demo_root), "--json"])
    assert demo.exit_code == 0, demo.output
    payload = json.loads(demo.output)
    assert len(payload["journey"]) == 7
    config_path = Path(payload["config_path"])
    prefix = ["--config", str(config_path)]

    expected_views = [
        (["status", "--verbose"], "CURRENT"),
        (["next", "--dry-run", "--verbose"], "NEXT RECOMMENDATION"),
        (["current", "--verbose"], "Public files"),
        (["readiness", "--verbose"], "Readiness"),
        (["report", "--verbose"], "IMPROVED"),
        (["report", "--format", "markdown"], "# Weekly Adaptive Tutor report"),
        (["history"], "Attempts"),
        (["concepts"], "Uncertainty"),
        (["hint"], "Hint 1/5"),
        (["doctor", "--offline"], "Adaptive Tutor doctor"),
    ]
    for arguments, expected in expected_views:
        result = runner.invoke(app, [*prefix, *arguments])
        assert result.exit_code == 0, (arguments, result.output)
        assert expected in result.output

    for arguments in (
        ["current", "--json"],
        ["readiness", "--json"],
        ["history", "--json"],
        ["concepts", "--json"],
        ["report", "--format", "json"],
        ["doctor", "--offline", "--json"],
    ):
        result = runner.invoke(app, [*prefix, *arguments])
        assert result.exit_code == 0, (arguments, result.output)
        assert json.loads(result.output) is not None

    backup_path = tmp_path / "backups" / "demo.sqlite3"
    backup = runner.invoke(app, [*prefix, "backup", str(backup_path)])
    assert backup.exit_code == 0, backup.output
    assert backup_path.is_file()
    unconfirmed = runner.invoke(app, [*prefix, "restore", str(backup_path)])
    assert unconfirmed.exit_code == 1
    restored = runner.invoke(
        app, [*prefix, "restore", str(backup_path), "--yes"]
    )
    assert restored.exit_code == 0, restored.output
    assert "integrity check passed" in restored.output

    loaded = runner.invoke(
        app, [*prefix, "curriculum-load", str(bundled_curriculum_path())]
    )
    assert loaded.exit_code == 0, loaded.output
    assert "Systems Foundations" in loaded.output

    class EmptyWorkerOrchestrator:
        def handlers(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(
        "adaptive_tutor.cli._orchestrator",
        lambda settings, database: EmptyWorkerOrchestrator(),
    )
    worker = runner.invoke(app, [*prefix, "worker", "--once"])
    assert worker.exit_code == 0, worker.output

    served: dict[str, object] = {}

    def fake_run(application: object, **options: object) -> None:
        served["application"] = application
        served.update(options)

    monkeypatch.setattr("adaptive_tutor.cli.uvicorn.run", fake_run)
    serve = runner.invoke(app, [*prefix, "serve"])
    assert serve.exit_code == 0, serve.output
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8765


def test_cli_remote_assignment_result_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    config_path = tmp_path / "config.yaml"
    prefix = ["--config", str(config_path)]
    initialized = runner.invoke(
        app, [*prefix, "init", "--data-dir", str(tmp_path / "state")]
    )
    assert initialized.exit_code == 0, initialized.output

    class StubOrchestrator:
        def __init__(self) -> None:
            self.result: dict[str, object] = {
                "existing": False,
                "id": "A-0042",
                "title": "Trace a bounded queue",
                "branch_name": "tutor/a-0042",
                "url": "https://github.com/example/learning/pull/42",
            }

        def create_next_assignment(self, context: object) -> dict[str, object]:
            assert context is not None
            return self.result

    stub = StubOrchestrator()
    monkeypatch.setattr(
        "adaptive_tutor.cli._orchestrator", lambda settings, database: stub
    )

    created = runner.invoke(app, [*prefix, "next"])
    assert created.exit_code == 0, created.output
    assert "Assignment created" in created.output
    assert "pull/42" in created.output

    as_json = runner.invoke(app, [*prefix, "next", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["id"] == "A-0042"

    stub.result = {
        "existing": True,
        "id": "A-0042",
        "title": "Trace a bounded queue",
    }
    existing = runner.invoke(app, [*prefix, "next"])
    assert existing.exit_code == 0, existing.output
    assert "remains active" in existing.output


def test_cli_grader_prebinds_owner_only_unix_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    socket_path = tmp_path / "grader" / "grader.sock"
    socket_path.parent.mkdir()
    socket_path.write_text("do not replace", encoding="utf-8")

    refused = runner.invoke(app, ["grader", "--socket", str(socket_path)])
    assert refused.exit_code == 1
    assert "Refusing to replace non-socket" in refused.output

    socket_path.unlink()
    captured: dict[str, object] = {}

    def fake_server(application: object, listener: socket.socket) -> None:
        captured["application"] = application
        captured["listener"] = listener
        info = socket_path.lstat()
        assert info.st_uid == os.geteuid()
        assert info.st_gid == os.getegid()
        assert info.st_mode & 0o777 == 0o600

    monkeypatch.setattr("adaptive_tutor.cli._run_grader_server", fake_server)
    started = runner.invoke(
        app,
        [
            "grader",
            "--socket",
            str(socket_path),
            "--model",
            "test-model",
            "--timeout-seconds",
            "45",
        ],
    )
    assert started.exit_code == 0, started.output
    assert captured["listener"] is not None
    assert socket_path.parent.stat().st_mode & 0o777 == 0o700
    assert not socket_path.exists()


def test_cli_grader_group_socket_has_exact_connect_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    socket_path = tmp_path / "grader" / "grader.sock"
    group = grp.getgrgid(os.getgid())
    observed: dict[str, int] = {}

    def fake_server(application: object, listener: socket.socket) -> None:
        assert application is not None
        directory = socket_path.parent.stat()
        info = socket_path.lstat()
        observed.update(
            directory_mode=directory.st_mode & 0o777,
            directory_gid=directory.st_gid,
            socket_mode=info.st_mode & 0o777,
            socket_gid=info.st_gid,
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(socket_path))
            accepted, _ = listener.accept()
            accepted.close()
        finally:
            client.close()

    monkeypatch.setattr("adaptive_tutor.cli._run_grader_server", fake_server)
    result = runner.invoke(
        app,
        [
            "grader",
            "--socket",
            str(socket_path),
            "--socket-group",
            group.gr_name,
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed == {
        "directory_mode": 0o750,
        "directory_gid": group.gr_gid,
        "socket_mode": 0o660,
        "socket_gid": group.gr_gid,
    }
    assert not socket_path.exists()


def test_cli_grader_rejects_unavailable_group_and_symlinked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    unavailable_gid = max({os.getgid(), os.getegid(), *os.getgroups()}) + 10_000
    monkeypatch.setattr(grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=unavailable_gid))
    denied = runner.invoke(
        app,
        [
            "grader",
            "--socket",
            str(tmp_path / "denied" / "grader.sock"),
            "--socket-group",
            "not-permitted",
        ],
    )
    assert denied.exit_code == 1
    assert "not a member" in denied.output

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    symlinked = runner.invoke(
        app,
        ["grader", "--socket", str(linked_directory / "grader.sock")],
    )
    assert symlinked.exit_code == 1
    assert "must not contain symlinks" in symlinked.output
