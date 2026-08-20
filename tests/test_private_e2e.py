from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from adaptive_tutor.config import GitHubSettings, TutorSettings
from adaptive_tutor.db import Database

ROOT = Path(__file__).resolve().parents[1]
DRIVER = runpy.run_path(str(ROOT / "scripts" / "prove-private-e2e"))


def _driver(name: str) -> Any:
    return DRIVER[name]


def test_private_proof_requires_explicit_resource_confirmation() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/prove-private-e2e"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--confirm-private-resources" in result.stdout


def test_private_proof_sanitizes_registered_values_and_tokens() -> None:
    private_values = _driver("_PRIVATE_VALUES")
    private_values.add("dedicated-private-resource")
    token = "gh" + "o_" + "a" * 24

    sanitized = _driver("_sanitize")(f"owner=dedicated-private-resource token={token}")

    assert sanitized == "owner=[private] token=[REDACTED]"


def test_private_proof_installs_digest_bound_hosted_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "tutor.sqlite3")
    database.migrate()
    settings = TutorSettings(
        data_dir=tmp_path / "state",
        github=GitHubSettings(owner="owner", workspace_repo="workspace"),
    )
    installed: dict[str, bytes] = {}

    class API:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def request(self, method: str, path: str) -> object:
            assert method == "GET"
            self.requested.append(path)
            return object()

        @staticmethod
        def repository(owner: str, workspace: str) -> dict[str, Any]:
            assert (owner, workspace) == ("owner", "workspace")
            return {
                "id": 123,
                "full_name": "owner/workspace",
                "default_branch": "main",
            }

    def replace(
        api: object,
        owner: str,
        workspace: str,
        files: dict[str, bytes],
        message: str,
    ) -> str:
        assert owner == "owner" and workspace == "workspace"
        assert "protected" in message.lower()
        installed.update(files)
        return "f" * 40

    install_control = _driver("_install_evaluator_control")
    driver_globals = install_control.__globals__
    monkeypatch.setitem(
        driver_globals,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 0, stdout="e" * 40 + "\n"),
    )
    monkeypatch.setitem(driver_globals, "_replace_default_tree", replace)
    monkeypatch.setitem(driver_globals, "_protect_main", lambda *_: True)

    api = API()
    protected = install_control(api, "owner", "workspace", settings, database)

    assert protected is True
    assert ".github/workflows/adaptive-tutor-evaluate.yml" in installed
    assert ".adaptive-tutor/evaluator-signing.pub" in installed
    assert b"PRIVATE" not in installed[".adaptive-tutor/evaluator-signing.pub"]
    control = database.fetch_one("SELECT * FROM evaluator_control_planes")
    assert control is not None
    assert control["repository_id"] == 123
    assert control["evaluator_ref"] == "e" * 40
    assert str(control["workflow_digest"]).startswith("sha256:")
    assert api.requested == [f"/repos/TGDivy/adaptive-tutor/commits/{'e' * 40}"]


def test_private_proof_has_no_self_hosted_runner_handoff() -> None:
    source = (ROOT / "scripts/prove-private-e2e").read_text(encoding="utf-8")

    assert "stage-evaluator" not in source
    assert "registration-token" not in source
    assert "stage-request" not in source
    assert "_run_ephemeral_runner" not in source


def test_private_proof_bootstraps_an_empty_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Response:
        status_code = 404

    class Client:
        @staticmethod
        def get(_path: str) -> Response:
            return Response()

    class API:
        client = Client()

        @staticmethod
        def request(method: str, path: str, **_kwargs: object) -> object:
            calls.append((method, path))

            class Initialized:
                @staticmethod
                def json() -> dict[str, object]:
                    return {"commit": {"sha": "a" * 40}}

            return Initialized()

    replace = _driver("_replace_default_tree")
    monkeypatch.setitem(replace.__globals__, "_remove_protection", lambda *_: None)
    monkeypatch.setitem(
        replace.__globals__,
        "_commit_tree",
        lambda *_args, **kwargs: str(kwargs["parent"]),
    )

    commit = replace(API(), "owner", "workspace", {"README.md": b"ok"}, "message")

    assert commit == "a" * 40
    assert calls == [
        ("PUT", "/repos/owner/workspace/contents/.adaptive-tutor-bootstrap"),
        ("PATCH", "/repos/owner/workspace"),
    ]
