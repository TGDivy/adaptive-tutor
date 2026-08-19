"""Credential-free deterministic evaluator for ephemeral CI runners."""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .errors import AssignmentValidationError, SecurityError
from .models import AssignmentBundle, AutomatedCheck, AutomatedEvaluation
from .security import assert_credentials_absent, untrusted_process_environment

MAX_SUBMISSION_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_BUNDLE_BYTES = 5 * 1024 * 1024


def evaluate_workspace_to_file(
    *,
    bundle_path: Path,
    workspace: Path,
    output_path: Path,
    assignment_id: str,
    commit_sha: str,
) -> AutomatedEvaluation:
    """Evaluate an untrusted checkout while keeping trusted inputs and output outside it."""
    trusted_bundle = bundle_path.expanduser().resolve(strict=True)
    untrusted_workspace = workspace.expanduser().resolve(strict=True)
    if not untrusted_workspace.is_dir():
        raise SecurityError("Evaluator workspace must be a directory")
    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_within(trusted_bundle, untrusted_workspace):
        raise SecurityError("Trusted evaluator bundle must be outside the learner workspace")
    if _is_within(destination, untrusted_workspace):
        raise SecurityError("Evidence output must be outside the learner workspace")
    if trusted_bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise SecurityError("Trusted evaluator bundle exceeds the size limit")
    try:
        bundle = AssignmentBundle.model_validate_json(
            trusted_bundle.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise AssignmentValidationError(f"Trusted evaluator bundle is invalid: {exc}") from exc
    evidence = CredentialFreeEvaluator().evaluate(
        bundle=bundle,
        assignment_id=assignment_id,
        commit_sha=commit_sha,
        workspace=untrusted_workspace,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".adaptive-tutor-evidence-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(evidence.model_dump(mode="json"), temporary, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return evidence


class CredentialFreeEvaluator:
    def evaluate(
        self,
        *,
        bundle: AssignmentBundle,
        assignment_id: str,
        commit_sha: str,
        workspace: Path,
    ) -> AutomatedEvaluation:
        started = datetime.now(UTC)
        started_clock = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="adaptive-tutor-evaluation-") as temporary:
            root = Path(temporary)
            self._copy_submission(bundle, workspace.resolve(), root)
            self._install_hidden_tests(bundle, root)
            environment = untrusted_process_environment(root)
            environment["PYTHONPATH"] = str(root)
            (root / "tmp").mkdir(mode=0o700)
            assert_credentials_absent(environment)
            command = self._command(bundle)
            output_path = root / "evaluation-output.txt"
            status = "error"
            summary = "Evaluator did not run"
            try:
                with output_path.open("wb") as output:
                    completed = subprocess.run(  # noqa: S603 - fixed interpreter and pytest module
                        command,
                        cwd=root,
                        env=environment,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                        check=False,
                        preexec_fn=_resource_limits if os.name == "posix" else None,
                    )
                status = "pass" if completed.returncode == 0 else "fail"
                summary = _bounded_output(output_path)
            except subprocess.TimeoutExpired:
                status = "error"
                summary = "Evaluation exceeded the 90 second limit"
            duration = int((time.monotonic() - started_clock) * 1000)
            checks = [
                AutomatedCheck(
                    name="submission boundary",
                    status="pass",
                    category="policy",
                    summary="Only bounded regular assignment files entered the evaluator",
                ),
                AutomatedCheck(
                    name="credential boundary",
                    status="pass",
                    category="policy",
                    summary="Evaluator environment contains no credential-like variables",
                ),
                AutomatedCheck(
                    name="public and hidden tests",
                    status=status,  # type: ignore[arg-type]
                    category="test",
                    summary=summary or ("Checks passed" if status == "pass" else "Checks failed"),
                    duration_ms=duration,
                ),
            ]
            evidence = AutomatedEvaluation(
                assignment_id=assignment_id,
                commit_sha=commit_sha,
                checks=checks,
                started_at=started,
                completed_at=datetime.now(UTC),
                runner="adaptive-tutor-credential-free-ci",
                artifact_digest="sha256:" + "0" * 64,
            )
            return evidence.with_computed_digest()

    @staticmethod
    def _copy_submission(bundle: AssignmentBundle, workspace: Path, root: Path) -> None:
        total = 0
        for item in bundle.files:
            if item.role not in {"instructions", "starter", "public_test"}:
                continue
            source = _safe_path(workspace, item.path)
            if not source.is_file() or source.is_symlink():
                raise SecurityError(f"Submission is missing a regular file: {item.path}")
            content = source.read_bytes()
            total += len(content)
            if total > MAX_SUBMISSION_BYTES:
                raise SecurityError("Submission exceeds the evaluator size limit")
            target = _safe_path(root, item.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    @staticmethod
    def _install_hidden_tests(bundle: AssignmentBundle, root: Path) -> None:
        by_path = {item.path: item for item in bundle.files}
        extras = bundle.hidden_evaluator.get("extra_tests", {})
        if not isinstance(extras, dict):
            raise AssignmentValidationError("extra_tests must be a mapping")
        for target_name, source_name in extras.items():
            source = by_path.get(str(source_name))
            if source is None or source.role != "evaluator":
                raise AssignmentValidationError(f"Missing trusted evaluator: {source_name}")
            target = _safe_path(root, str(target_name))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.content, encoding="utf-8")

    @staticmethod
    def _command(bundle: AssignmentBundle) -> list[str]:
        if bundle.validation_command[:3] != ["python", "-m", "pytest"] or any(
            argument not in {"-q"} for argument in bundle.validation_command[3:]
        ):
            raise AssignmentValidationError("Only the fixed Python pytest harness is supported")
        return [sys.executable, *bundle.validation_command[1:]]


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        raise SecurityError(f"Path escapes evaluator workspace: {relative}")
    return candidate


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    if hasattr(resource, "RLIMIT_AS"):
        resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    if hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))


def _bounded_output(path: Path) -> str:
    with path.open("rb") as source:
        content = source.read(MAX_OUTPUT_BYTES + 1)
    if len(content) > MAX_OUTPUT_BYTES:
        content = content[:MAX_OUTPUT_BYTES]
    text = content.decode("utf-8", errors="replace").strip()
    lines = [line for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-8:])[-3000:]
