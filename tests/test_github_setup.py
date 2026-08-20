from __future__ import annotations

import base64
import json
import stat
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from adaptive_tutor.config import load_settings, write_initial_config
from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.dashboard import create_app
from adaptive_tutor.db import Database
from adaptive_tutor.errors import ConfigurationError, SecurityError
from adaptive_tutor.github_setup import (
    EvaluatorControlProvisioner,
    GitHubAppSetupService,
    GitHubCLIBootstrap,
    InstalledEvaluatorControls,
    PublicEvaluatorSource,
)
from adaptive_tutor.runner import evaluator_kit_digest
from adaptive_tutor.security import sha256_digest
from adaptive_tutor.setup import SetupRun, SetupService, StepOutcome


class SetupUntilGitHubApp:
    def execute(self, step: str, run: SetupRun) -> StepOutcome:
        if step == "github_repository":
            return StepOutcome.complete(
                "private repository created",
                external_ids={
                    "repository_id": 9876,
                    "owner_type": "User",
                    "repository": "example-owner/learning-workspace",
                },
            )
        if step == "github_app":
            return StepOutcome.wait(
                "browser approval required",
                action=f"Open {run.public_url}/setup/github-app",
            )
        return StepOutcome.complete(f"{step} complete")


def configured_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any, Database, SetupRun]:
    monkeypatch.delenv("ADAPTIVE_TUTOR_API_TOKEN", raising=False)
    monkeypatch.delenv("ADAPTIVE_TUTOR_WEBHOOK_SECRET", raising=False)
    config_path = tmp_path / "config.yaml"
    write_initial_config(
        config_path,
        data_dir=tmp_path / "state",
        github_owner="example-owner",
        workspace_repo="learning-workspace",
        webhook_url="https://tutor.example.test",
    )
    settings = load_settings(config_path, require_file=True)
    database = Database(settings.database_path or tmp_path / "state" / "tutor.sqlite3")
    database.migrate()
    CurriculumLoader().persist(
        CurriculumLoader().load(bundled_curriculum_path()),
        database,
        settings.learner_id,
    )
    setup = SetupService(database)
    setup.begin(
        public_url="https://tutor.example.test",
        goal_statement="Build reliable network services.",
        config_path=config_path,
        learner_id=settings.learner_id,
        curriculum_id=settings.active_curriculum,
    )
    return config_path, settings, database, setup.resume(SetupUntilGitHubApp())


def test_github_cli_bootstrap_discovers_owner_and_creates_private_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    repository_reads = 0

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        nonlocal repository_reads
        calls.append(command)
        assert options["env"]["GH_HOST"] == "github.com"
        arguments = command[1:]
        if arguments[:3] == ["auth", "status", "--hostname"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if arguments == ["api", "user", "--jq", ".login"]:
            return subprocess.CompletedProcess(command, 0, "example-owner\n", "")
        if arguments == ["api", "users/example-owner", "--jq", ".type"]:
            return subprocess.CompletedProcess(command, 0, "User\n", "")
        if arguments == ["api", "repos/example-owner/learning-workspace"]:
            repository_reads += 1
            if repository_reads == 1:
                return subprocess.CompletedProcess(command, 1, "", "HTTP 404")
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "id": 9876,
                        "full_name": "example-owner/learning-workspace",
                        "private": True,
                        "html_url": "https://github.com/example-owner/learning-workspace",
                    }
                ),
                "",
            )
        if arguments[:2] == ["repo", "create"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(arguments)

    monkeypatch.setattr("adaptive_tutor.github_setup.subprocess.run", run)
    repository = GitHubCLIBootstrap("/usr/bin/gh").ensure_private_repository(
        "", "learning-workspace"
    )

    assert repository.repository_id == 9876
    assert repository.owner == "example-owner"
    assert repository.owner_type == "User"
    assert any(call[1:3] == ["repo", "create"] for call in calls)


def test_github_cli_bootstrap_rejects_public_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        arguments = command[1:]
        if arguments[:2] == ["auth", "status"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if arguments[:2] == ["api", "users/example-owner"]:
            return subprocess.CompletedProcess(command, 0, "User\n", "")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "id": 9876,
                    "full_name": "example-owner/learning-workspace",
                    "private": False,
                    "html_url": "https://github.com/example-owner/learning-workspace",
                }
            ),
            "",
        )

    monkeypatch.setattr("adaptive_tutor.github_setup.subprocess.run", run)
    with pytest.raises(SecurityError, match="must be private"):
        GitHubCLIBootstrap("/usr/bin/gh").ensure_private_repository(
            "example-owner", "learning-workspace"
        )


def test_github_cli_installs_and_reads_back_protected_evaluator_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow = "name: protected evaluator\n"
    verification_key = "ed25519:" + "ab" * 32 + "\n"
    uploaded: dict[str, dict[str, Any]] = {}
    protection: dict[str, Any] = {}

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        arguments = command[1:]
        if arguments == ["api", "repos/example-owner/learning-workspace"]:
            payload = {
                "id": 9876,
                "full_name": "example-owner/learning-workspace",
                "private": True,
                "default_branch": "main",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if "contents/.github/workflows/adaptive-tutor-evaluate.yml" in " ".join(arguments):
            if "PUT" in arguments:
                uploaded["workflow"] = json.loads(options["input"])
                return subprocess.CompletedProcess(command, 0, "{}", "")
            return subprocess.CompletedProcess(command, 1, "", "HTTP 404")
        if "contents/.adaptive-tutor/evaluator-signing.pub" in " ".join(arguments):
            if "PUT" in arguments:
                uploaded["key"] = json.loads(options["input"])
                return subprocess.CompletedProcess(command, 0, "{}", "")
            return subprocess.CompletedProcess(command, 1, "", "HTTP 404")
        if arguments[-2:] == ["--input", "-"] and "protection" in arguments[-3]:
            protection.update(json.loads(options["input"]))
            return subprocess.CompletedProcess(command, 0, "{}", "")
        if arguments == [
            "api",
            "repos/example-owner/learning-workspace/git/ref/heads/main",
        ]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"object": {"sha": "f" * 40}}), ""
            )
        if arguments == [
            "api",
            "repos/example-owner/learning-workspace/branches/main/protection",
        ]:
            response = {
                "required_pull_request_reviews": {"required_approving_review_count": 1},
                "enforce_admins": {"enabled": True},
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(response), "")
        raise AssertionError(arguments)

    monkeypatch.setattr("adaptive_tutor.github_setup.subprocess.run", run)
    installed = GitHubCLIBootstrap("/usr/bin/gh").install_evaluator_controls(
        owner="example-owner",
        repository="learning-workspace",
        workflow=workflow,
        verification_key=verification_key,
    )

    assert installed.workflow_commit == "f" * 40
    assert installed.branch_protected is True
    assert base64.b64decode(uploaded["workflow"]["content"]).decode() == workflow
    assert base64.b64decode(uploaded["key"]["content"]).decode() == verification_key
    assert protection["enforce_admins"] is True
    assert protection["allow_force_pushes"] is False
    assert protection["required_pull_request_reviews"]["required_approving_review_count"] == 1


def test_evaluator_control_provisioner_binds_remote_source_and_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, settings, database, run = configured_setup(tmp_path, monkeypatch)
    settings.github.evaluator_ref = "e" * 40
    workflow = "name: protected evaluator\n"

    class Bootstrap:
        def public_evaluator_source(self, revision: str) -> PublicEvaluatorSource:
            assert revision == "e" * 40
            return PublicEvaluatorSource(revision, workflow, evaluator_kit_digest())

        def install_evaluator_controls(self, **values: Any) -> InstalledEvaluatorControls:
            assert values["owner"] == "example-owner"
            assert values["repository"] == "learning-workspace"
            assert values["workflow"] == workflow
            assert str(values["verification_key"]).startswith("ed25519:")
            return InstalledEvaluatorControls(
                repository_id=9876,
                repository_full_name="example-owner/learning-workspace",
                default_branch="main",
                workflow_commit="f" * 40,
                branch_protected=True,
            )

    class InstalledAppClient:
        def __init__(self, github: Any) -> None:
            assert github.evaluator_ref == "e" * 40

        def verify_evaluator_control(self, **expected: Any) -> dict[str, str | int]:
            assert expected["expected_repository_id"] == 9876
            assert expected["expected_workflow_digest"] == sha256_digest(workflow)
            return {
                "repository_id": 9876,
                "repository_full_name": "example-owner/learning-workspace",
                "default_branch": "main",
                "workflow_commit": "f" * 40,
                "workflow_digest": sha256_digest(workflow),
                "evaluator_key_id": str(expected["expected_key_id"]),
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr("adaptive_tutor.github_setup.GitHubClient", InstalledAppClient)
    stored = EvaluatorControlProvisioner(
        settings,
        database,
        config_path,
        bootstrap=Bootstrap(),  # type: ignore[arg-type]
    ).ensure(run)

    assert stored["repository_id"] == 9876
    assert stored["workflow_digest"] == sha256_digest(workflow)
    assert stored["evaluator_ref"] == "e" * 40
    assert stored["evaluator_kit_digest"] == evaluator_kit_digest()
    assert len(str(stored["evaluator_key_id"])) == 16
    assert (
        stat.S_IMODE((settings.data_dir / "trusted-evaluators" / "signing.key").stat().st_mode)
        == 0o600
    )


def test_github_app_manifest_flow_persists_owner_only_credentials_and_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, settings, database, run = configured_setup(tmp_path, monkeypatch)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()

    def conversion(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app-manifests/manifest-code-123/conversions"
        return httpx.Response(
            201,
            json={
                "id": 12345,
                "slug": "adaptive-tutor-example",
                "pem": pem,
                "webhook_secret": "webhook-secret-from-github-123456",
                "owner": {"login": "example-owner"},
            },
        )

    service = GitHubAppSetupService(
        settings,
        database,
        config_path,
        transport=httpx.MockTransport(conversion),
    )
    launch = service.start(run)
    manifest_state = parse_qs(urlparse(launch.action_url).query)["state"][0]
    manifest = json.loads(launch.manifest_json)
    assert launch.action_url.startswith("https://github.com/settings/apps/new?")
    assert manifest["public"] is False
    assert manifest["hook_attributes"]["url"].endswith("/webhooks/github")
    assert manifest["default_permissions"] == {
        "actions": "write",
        "checks": "read",
        "contents": "write",
        "issues": "write",
        "metadata": "read",
        "pull_requests": "write",
    }

    installation_url = service.complete_manifest(
        run,
        code="manifest-code-123",
        state=manifest_state,
    )
    installation_state = parse_qs(urlparse(installation_url).query)["state"][0]
    key_path = settings.data_dir / "github-app.pem"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert "PRIVATE KEY" in key_path.read_text(encoding="utf-8")
    assert "webhook-secret-from-github" not in config_path.read_text(encoding="utf-8")
    assert "webhook-secret-from-github-123456" in settings.secrets_file.read_text(encoding="utf-8")

    observed: dict[str, Any] = {}

    class InstalledAppClient:
        def __init__(self, github: Any) -> None:
            observed["app_id"] = github.app_id
            observed["installation_id"] = github.installation_id

        def verify_private_repository(self) -> dict[str, Any]:
            return {
                "id": 9876,
                "private": True,
                "full_name": "example-owner/learning-workspace",
                "default_branch": "main",
                "permissions": {"push": True},
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr("adaptive_tutor.github_setup.GitHubClient", InstalledAppClient)
    repository = service.complete_installation(
        run,
        installation_id=67890,
        state=installation_state,
    )
    service.close()

    assert repository["id"] == 9876
    assert observed == {"app_id": 12345, "installation_id": 67890}
    reloaded = load_settings(config_path, require_file=True)
    assert reloaded.github.app_id == 12345
    assert reloaded.github.installation_id == 67890
    assert reloaded.github.private_key_path == key_path
    session = database.fetch_one("SELECT phase, status FROM github_app_setup_sessions")
    assert session == {"phase": "installation", "status": "complete"}
    with pytest.raises(ConfigurationError, match="invalid or expired"):
        service.complete_installation(run, installation_id=67890, state=installation_state)


def test_authenticated_setup_page_posts_manifest_to_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, settings, database, _ = configured_setup(tmp_path, monkeypatch)
    token = settings.api_token
    assert token is not None
    app = create_app(settings, database, config_path=config_path)

    with TestClient(app) as client:
        assert client.get("/setup", follow_redirects=False).status_code == 303
        assert client.post("/login", data={"token": token}).status_code == 200
        status_page = client.get("/setup")
        assert status_page.status_code == 200
        assert "Setup required" in status_page.text
        app_page = client.get("/setup/github-app")
        assert app_page.status_code == 200
        assert 'action="https://github.com/settings/apps/new?state=' in app_page.text
        assert 'name="manifest"' in app_page.text
        assert "webhook_secret" not in app_page.text
        assert (
            "form-action 'self' https://github.com" in app_page.headers["content-security-policy"]
        )
