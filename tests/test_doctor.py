from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from adaptive_tutor.config import CodexSettings, GitHubSettings, TutorSettings
from adaptive_tutor.db import Database
from adaptive_tutor.doctor import Doctor


def test_offline_doctor_reports_actionable_local_state(
    initialized: tuple[Database, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = initialized
    settings = TutorSettings(
        data_dir=tmp_path / "private-state",
        database_path=database.path,
        learner_id="learner",
        codex=CodexSettings(enabled=False),
    )
    settings.ensure_runtime_dirs()

    def unavailable(*_: object, **__: object) -> None:
        raise httpx.ConnectError("service is stopped")

    monkeypatch.setattr("adaptive_tutor.doctor.httpx.get", unavailable)
    checks = {item.name: item for item in Doctor(settings, database).run(online=False)}

    assert checks["Database"].status == "pass"
    assert checks["Configuration"].status == "pass"
    assert checks["Filesystem permissions"].status == "pass"
    assert checks["Codex CLI"].status == "warn"
    assert checks["GitHub App configuration"].status == "warn"
    assert checks["GitHub connectivity"].status == "warn"
    assert checks["Service health"].status == "warn"
    assert all(item.detail for item in checks.values())


def test_doctor_rejects_world_readable_private_state(
    database: Database, tmp_path: Path
) -> None:
    data_dir = tmp_path / "private-state"
    settings = TutorSettings(data_dir=data_dir, database_path=database.path)
    settings.ensure_runtime_dirs()
    data_dir.chmod(0o755)

    check = Doctor(settings, database)._filesystem()
    assert check.status == "fail"
    assert str(data_dir) in check.detail
    assert check.fix


def test_doctor_reports_transport_setup_errors_without_crashing(
    initialized: tuple[Database, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = initialized
    settings = TutorSettings(
        data_dir=tmp_path / "private-state",
        database_path=database.path,
        learner_id="learner",
        codex=CodexSettings(enabled=False),
    )
    settings.ensure_runtime_dirs()
    monkeypatch.setattr(
        "adaptive_tutor.doctor.httpx.get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("certificate unreadable")),
    )

    check = Doctor(settings, database)._service()

    assert check.status == "warn"
    assert "certificate unreadable" in check.detail
    assert check.fix


def test_doctor_database_and_configuration_failure_diagnostics(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = TutorSettings(data_dir=tmp_path / "state", database_path=database.path)
    doctor = Doctor(settings, database)

    missing_curriculum = doctor._configuration()
    assert missing_curriculum.status == "fail"
    assert "not loaded" in missing_curriculum.detail

    fresh = Database(tmp_path / "fresh" / "tutor.sqlite3")
    applied = Doctor(
        TutorSettings(data_dir=tmp_path / "fresh", database_path=fresh.path), fresh
    )._database()
    assert applied.status == "pass"
    assert "applied" in applied.detail

    monkeypatch.setattr(database, "integrity_check", lambda: (False, "corrupt"))
    unhealthy = doctor._database()
    assert unhealthy.status == "fail"
    assert unhealthy.detail == "corrupt"

    monkeypatch.setattr(
        database,
        "migrate",
        lambda: (_ for _ in ()).throw(PermissionError("database is read-only")),
    )
    inaccessible = doctor._database()
    assert inaccessible.status == "fail"
    assert "read-only" in inaccessible.detail


def test_doctor_codex_tooling_and_github_configuration_branches(
    initialized: tuple[Database, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = initialized
    key_path = tmp_path / "github-app.pem"
    key_path.write_text("key", encoding="utf-8")
    settings = TutorSettings(
        data_dir=tmp_path / "state",
        database_path=database.path,
        github=GitHubSettings(
            owner="example-owner",
            app_id=11,
            installation_id=22,
            private_key_path=key_path,
        ),
        codex=CodexSettings(enabled=True, command="codex-test"),
    )
    doctor = Doctor(settings, database)

    monkeypatch.setattr("adaptive_tutor.doctor.shutil.which", lambda _name: None)
    assert doctor._codex().status == "fail"
    assert doctor._tooling().status == "fail"

    monkeypatch.setattr(
        "adaptive_tutor.doctor.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"codex-test", "git", "python"} else None,
    )
    assert doctor._codex().status == "pass"
    tooling = doctor._tooling()
    assert tooling.status == "pass"
    assert "optional: none" in tooling.detail

    monkeypatch.delenv("ADAPTIVE_TUTOR_WEBHOOK_SECRET", raising=False)
    missing_secret = doctor._github_configuration()
    assert missing_secret.status == "fail"
    assert "Webhook secret" in missing_secret.detail
    monkeypatch.setenv("ADAPTIVE_TUTOR_WEBHOOK_SECRET", "webhook-test-secret")
    assert doctor._github_configuration().status == "pass"

    missing_key_settings = settings.model_copy(deep=True)
    missing_key_settings.github.private_key_path = tmp_path / "missing.pem"
    assert Doctor(missing_key_settings, database)._github_configuration().status == "fail"

    monkeypatch.setenv("ADAPTIVE_TUTOR_GITHUB_TOKEN", "development-token")
    token_settings = settings.model_copy(deep=True)
    token_settings.github.app_id = None
    token_settings.github.installation_id = None
    token_check = Doctor(token_settings, database)._github_configuration()
    assert token_check.status == "warn"
    assert "development token" in token_check.detail


@pytest.mark.parametrize(
    ("webhook_url", "webhook_status", "expected"),
    [
        (None, None, "warn"),
        (
            "https://tutor.example.test",
            {
                "id": 17,
                "active": True,
                "events": [
                    "push",
                    "pull_request",
                    "workflow_run",
                    "check_suite",
                    "issue_comment",
                ],
            },
            "pass",
        ),
        ("https://tutor.example.test", None, "fail"),
    ],
)
def test_doctor_online_github_and_webhook_diagnostics(
    webhook_url: str | None,
    webhook_status: dict[str, Any] | None,
    expected: str,
    initialized: tuple[Database, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = initialized
    closed = False

    class StubGitHub:
        def verify_private_repository(self) -> dict[str, Any]:
            return {"full_name": "example-owner/learning-workspace"}

        def webhook_status(self, callback: str) -> dict[str, Any] | None:
            assert callback == "https://tutor.example.test/webhooks/github"
            return webhook_status

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        "adaptive_tutor.doctor.GitHubClient", lambda _settings: StubGitHub()
    )
    settings = TutorSettings(
        data_dir=tmp_path / "state",
        database_path=database.path,
        github=GitHubSettings(owner="example-owner", webhook_url=webhook_url),
    )
    checks = Doctor(settings, database)._github_online()
    assert checks[0].status == "pass"
    assert checks[1].status == expected
    assert closed is True


def test_doctor_online_and_service_failures_are_contained(
    initialized: tuple[Database, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = initialized
    closed = False

    class FailingGitHub:
        def verify_private_repository(self) -> dict[str, Any]:
            raise RuntimeError("installation unavailable")

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        "adaptive_tutor.doctor.GitHubClient", lambda _settings: FailingGitHub()
    )
    settings = TutorSettings(
        data_dir=tmp_path / "state",
        database_path=database.path,
        github=GitHubSettings(owner="example-owner"),
    )
    github = Doctor(settings, database)._github_online()
    assert github[0].status == "fail"
    assert "installation unavailable" in github[0].detail
    assert closed is True

    monkeypatch.setattr(
        "adaptive_tutor.doctor.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(200),
    )
    assert Doctor(settings, database)._service().status == "pass"
    monkeypatch.setattr(
        "adaptive_tutor.doctor.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(503),
    )
    unavailable = Doctor(settings, database)._service()
    assert unavailable.status == "fail"
    assert "503" in unavailable.detail
