"""Short-lived, schema-constrained Codex qualitative grading workers."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from .config import CodexSettings
from .db import Database
from .errors import ModelError, ModelSchemaError
from .models import QualitativeEvaluation
from .security import codex_worker_environment, redact, sha256_digest
from .time import iso_now


class QualitativeGrader(Protocol):
    def grade(
        self, prompt: str, *, prompt_version: str, purpose: str = "grading"
    ) -> QualitativeEvaluation:
        ...


class CodexRunner:
    def __init__(self, settings: CodexSettings, database: Database) -> None:
        self.settings = settings
        self.database = database

    def grade(
        self, prompt: str, *, prompt_version: str, purpose: str = "grading"
    ) -> QualitativeEvaluation:
        invocation_id = str(uuid.uuid4())
        started_at = iso_now()
        started = time.monotonic()
        self.database.execute(
            """
            INSERT INTO model_invocations(
                id, purpose, model, prompt_version, input_digest, status, started_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                invocation_id,
                purpose,
                self.settings.model,
                prompt_version,
                sha256_digest(prompt),
                started_at,
            ),
        )
        try:
            result, usage = self._invoke(prompt)
        except (ModelError, ModelSchemaError) as exc:
            self._record_failure(invocation_id, exc, started)
            raise
        cost = (
            usage["input_tokens"] * self.settings.usd_per_million_input_tokens
            + usage["output_tokens"] * self.settings.usd_per_million_output_tokens
        ) / 1_000_000
        self.database.execute(
            """
            UPDATE model_invocations SET output_digest=?, input_tokens=?, output_tokens=?,
                cost_usd=?, status='succeeded', completed_at=?, duration_ms=? WHERE id=?
            """,
            (
                sha256_digest(result.model_dump_json()),
                usage["input_tokens"],
                usage["output_tokens"],
                cost,
                iso_now(),
                int((time.monotonic() - started) * 1000),
                invocation_id,
            ),
        )
        return result

    def _invoke(self, prompt: str) -> tuple[QualitativeEvaluation, dict[str, int]]:
        executable = shutil.which(self.settings.command)
        if executable is None:
            raise ModelError(
                f"Codex CLI is unavailable: {self.settings.command}. Install and authenticate it."
            )
        with tempfile.TemporaryDirectory(prefix="adaptive-tutor-codex-") as temporary:
            root = Path(temporary)
            schema_path = root / "evaluation.schema.json"
            output_path = root / "evaluation.json"
            schema_path.write_text(
                json.dumps(QualitativeEvaluation.model_json_schema(), sort_keys=True),
                encoding="utf-8",
            )
            command = [
                executable,
                "--ask-for-approval",
                "never",
                "exec",
                "--ephemeral",
                "--sandbox",
                self.settings.sandbox,
                "--skip-git-repo-check",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if self.settings.model:
                command[1:1] = ["--model", self.settings.model]
            try:
                completed = subprocess.run(  # noqa: S603 - executable resolved; argv only
                    command,
                    cwd=root,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.settings.timeout_seconds,
                    env=codex_worker_environment(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ModelError(
                    f"Codex grading exceeded {self.settings.timeout_seconds} seconds",
                    retryable=True,
                ) from exc
            if completed.returncode != 0:
                diagnostic = redact((completed.stderr or completed.stdout)[-3000:])
                raise ModelError(
                    f"Codex grading failed with exit {completed.returncode}: {diagnostic}",
                    retryable=True,
                )
            if not output_path.is_file():
                raise ModelSchemaError("Codex completed without a structured output file")
            try:
                evaluation = QualitativeEvaluation.model_validate_json(
                    output_path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError, ValueError) as exc:
                raise ModelSchemaError(f"Codex output failed schema validation: {exc}") from exc
            return evaluation, _parse_usage(completed.stdout)

    def _record_failure(self, invocation_id: str, error: Exception, started: float) -> None:
        kind = "schema_failure" if isinstance(error, ModelSchemaError) else "model_failure"
        self.database.execute(
            """
            UPDATE model_invocations SET status='failed', failure_kind=?, completed_at=?,
                duration_ms=?, error=? WHERE id=?
            """,
            (
                kind,
                iso_now(),
                int((time.monotonic() - started) * 1000),
                redact(str(error))[:4000],
                invocation_id,
            ),
        )


class FixtureCodexRunner:
    """Deterministic local demo path; never used as live grading evidence."""

    def __init__(self, fixture: QualitativeEvaluation) -> None:
        self.fixture = fixture
        self.prompts: list[str] = []

    def grade(
        self, prompt: str, *, prompt_version: str, purpose: str = "grading"
    ) -> QualitativeEvaluation:
        self.prompts.append(prompt)
        return self.fixture


def _parse_usage(json_lines: str) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    for line in json_lines.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidates = [event]
        if isinstance(event, dict):
            candidates.extend(value for value in event.values() if isinstance(value, dict))
        for item in candidates:
            usage = item.get("usage") if isinstance(item, dict) else None
            if not isinstance(usage, dict):
                continue
            input_tokens = max(
                input_tokens,
                int(usage.get("input_tokens", usage.get("inputTokens", 0)) or 0),
            )
            output_tokens = max(
                output_tokens,
                int(usage.get("output_tokens", usage.get("outputTokens", 0)) or 0),
            )
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}
