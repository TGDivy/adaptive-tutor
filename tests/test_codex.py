from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from adaptive_tutor.codex import (
    CodexProcess,
    CodexRunner,
    GraderFailure,
    GraderResponse,
    GraderUsage,
)
from adaptive_tutor.config import CodexSettings
from adaptive_tutor.db import Database
from adaptive_tutor.errors import ModelError, ModelSchemaError
from adaptive_tutor.grader import MAX_GRADER_REQUEST_BYTES, create_grader_app
from adaptive_tutor.models import QualitativeEvaluation


def fixture_payload() -> dict[str, Any]:
    path = Path("curricula/systems-foundations/fixtures/demo-evaluation.json")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def isolated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> CodexSettings:
    socket_path = tmp_path / "grader.sock"
    server_socket = socket.socket(socket.AF_UNIX)
    server_socket.bind(str(socket_path))
    server_socket.close()
    monkeypatch.setattr(
        "adaptive_tutor.codex.httpx.HTTPTransport",
        lambda **_kwargs: httpx.MockTransport(handler),
    )
    return CodexSettings(socket_path=socket_path)


def test_codex_runner_uses_ephemeral_read_only_schema_contract(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(command=command, kwargs=kwargs)
        schema = Path(command[command.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema.read_text(encoding="utf-8"))
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps(fixture_payload()), encoding="utf-8")
        stdout = json.dumps({"usage": {"input_tokens": 120, "output_tokens": 80}})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("adaptive_tutor.codex.shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("adaptive_tutor.codex.subprocess.run", fake_run)
    process = CodexProcess(
        CodexSettings(usd_per_million_input_tokens=2, usd_per_million_output_tokens=8)
    )
    result, usage = process.invoke("trusted prompt")
    assert isinstance(result, QualitativeEvaluation)
    command = captured["command"]
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert "--output-schema" in command
    concept_schema = captured["schema"]["$defs"]["ConceptEvidence"]
    assert set(concept_schema["required"]) == set(concept_schema["properties"])
    assert "transfer_context" in concept_schema["required"]
    assert "default" not in concept_schema["properties"]["transfer_context"]
    assert concept_schema["additionalProperties"] is False
    assert captured["kwargs"]["input"] == "trusted prompt"
    assert captured["kwargs"]["start_new_session"] is True
    environment = captured["kwargs"]["env"]
    assert "ADAPTIVE_TUTOR_GITHUB_TOKEN" not in environment
    assert usage == {"input_tokens": 120, "output_tokens": 80}


def test_codex_runner_uses_only_isolated_socket_and_records_usage(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = GraderResponse(
        evaluation=QualitativeEvaluation.model_validate(fixture_payload()),
        usage=GraderUsage(input_tokens=120, output_tokens=80),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/grade"
        assert json.loads(request.content) == {"prompt": "trusted prompt"}
        return httpx.Response(200, json=response.model_dump(mode="json"))

    settings = isolated_settings(tmp_path, monkeypatch, handler).model_copy(
        update={
            "usd_per_million_input_tokens": 2,
            "usd_per_million_output_tokens": 8,
        }
    )
    result = CodexRunner(settings, database).grade("trusted prompt", prompt_version="v1")

    assert isinstance(result, QualitativeEvaluation)
    invocation = database.fetch_one("SELECT * FROM model_invocations")
    assert invocation is not None
    assert invocation["status"] == "succeeded"
    assert invocation["input_tokens"] == 120
    assert invocation["output_tokens"] == 80
    assert invocation["cost_usd"] > 0


def test_malformed_codex_output_never_becomes_evidence(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = GraderFailure(
        kind="schema_failure",
        detail="Codex output failed schema validation",
        retryable=False,
    )

    settings = isolated_settings(
        tmp_path,
        monkeypatch,
        lambda _request: httpx.Response(422, json=failure.model_dump()),
    )
    with pytest.raises(ModelSchemaError, match="schema validation"):
        CodexRunner(settings, database).grade("prompt", prompt_version="v1")
    assert database.fetch_one("SELECT status, failure_kind FROM model_invocations") == {
        "status": "failed",
        "failure_kind": "schema_failure",
    }
    assert database.fetch_one("SELECT COUNT(*) count FROM mastery_evidence") == {"count": 0}


def test_codex_failure_preserves_both_diagnostic_streams(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            2,
            stdout="structured output rejected",
            stderr="non-fatal setup warning",
        )

    monkeypatch.setattr("adaptive_tutor.codex.shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("adaptive_tutor.codex.subprocess.run", fake_run)
    with pytest.raises(ModelError) as raised:
        CodexProcess(CodexSettings()).invoke("prompt")
    assert "structured output rejected" in str(raised.value)
    assert "non-fatal setup warning" in str(raised.value)


def test_grader_service_bounds_requests_and_classifies_model_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation = QualitativeEvaluation.model_validate(fixture_payload())
    monkeypatch.setattr(
        "adaptive_tutor.grader.CodexProcess.invoke",
        lambda _self, prompt: (evaluation, {"input_tokens": len(prompt), "output_tokens": 7}),
    )
    with TestClient(create_grader_app(CodexSettings())) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        response = client.post("/v1/grade", json={"prompt": "bounded prompt"})
        assert response.status_code == 200
        assert response.json()["usage"] == {"input_tokens": 14, "output_tokens": 7}
        assert client.post("/v1/grade", content=b"not-json").status_code == 422
        oversized = client.post("/v1/grade", content=b"x" * (MAX_GRADER_REQUEST_BYTES + 1))
        assert oversized.status_code == 413

    def model_failure(_self: object, _prompt: str) -> tuple[QualitativeEvaluation, dict[str, int]]:
        raise ModelError("temporary model outage", retryable=True)

    monkeypatch.setattr("adaptive_tutor.grader.CodexProcess.invoke", model_failure)
    with TestClient(create_grader_app(CodexSettings())) as client:
        failure = client.post("/v1/grade", json={"prompt": "bounded prompt"})
    assert failure.status_code == 502
    assert failure.json() == {
        "kind": "model_failure",
        "detail": "temporary model outage",
        "retryable": True,
    }


def test_stateful_runner_refuses_direct_codex_execution(database: Database) -> None:
    with pytest.raises(ModelError, match="isolated Codex grader socket"):
        CodexRunner(CodexSettings(), database).grade("prompt", prompt_version="v1")
    invocation = database.fetch_one("SELECT status, failure_kind FROM model_invocations")
    assert invocation == {"status": "failed", "failure_kind": "model_failure"}
