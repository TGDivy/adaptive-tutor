from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from adaptive_tutor.codex import CodexRunner
from adaptive_tutor.config import CodexSettings
from adaptive_tutor.db import Database
from adaptive_tutor.errors import ModelError, ModelSchemaError
from adaptive_tutor.models import QualitativeEvaluation


def fixture_payload() -> dict[str, Any]:
    path = Path("curricula/systems-foundations/fixtures/demo-evaluation.json")
    return json.loads(path.read_text(encoding="utf-8"))


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
    runner = CodexRunner(
        CodexSettings(
            usd_per_million_input_tokens=2,
            usd_per_million_output_tokens=8,
        ),
        database,
    )
    result = runner.grade("trusted prompt", prompt_version="v1")
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
    environment = captured["kwargs"]["env"]
    assert "ADAPTIVE_TUTOR_GITHUB_TOKEN" not in environment
    invocation = database.fetch_one("SELECT * FROM model_invocations")
    assert invocation is not None
    assert invocation["status"] == "succeeded"
    assert invocation["input_tokens"] == 120
    assert invocation["output_tokens"] == 80
    assert invocation["cost_usd"] > 0


def test_malformed_codex_output_never_becomes_evidence(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"overall_score": 999}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("adaptive_tutor.codex.shutil.which", lambda _: "/usr/bin/codex")
    monkeypatch.setattr("adaptive_tutor.codex.subprocess.run", fake_run)
    with pytest.raises(ModelSchemaError, match="schema validation"):
        CodexRunner(CodexSettings(), database).grade("prompt", prompt_version="v1")
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
        CodexRunner(CodexSettings(), database).grade("prompt", prompt_version="v1")
    assert "structured output rejected" in str(raised.value)
    assert "non-fatal setup warning" in str(raised.value)
