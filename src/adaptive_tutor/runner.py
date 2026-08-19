"""Credential-free deterministic evaluator for ephemeral CI runners."""

from __future__ import annotations

import json
import os
import resource
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from .errors import AssignmentValidationError, SecurityError
from .models import AssignmentBundle, AutomatedCheck, AutomatedEvaluation
from .security import assert_credentials_absent, untrusted_process_environment
from .trusted_bundles import TrustedBundleEnvelope, read_provisioned_envelope

MAX_SUBMISSION_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ASSIGNMENT_METADATA_BYTES = 64 * 1024


def evaluate_workspace_to_file(
    *,
    bundle_path: Path,
    verification_key_path: Path,
    workspace: Path,
    output_path: Path,
    assignment_id: str,
    branch: str,
    commit_sha: str,
) -> AutomatedEvaluation:
    """Evaluate an untrusted checkout while keeping trusted inputs and output outside it."""
    trusted_bundle = bundle_path.expanduser().absolute()
    trusted_key = verification_key_path.expanduser().absolute()
    workspace_input = workspace.expanduser().absolute()
    if workspace_input.is_symlink():
        raise SecurityError("Evaluator workspace must not be a symlink")
    untrusted_workspace = workspace_input.resolve(strict=True)
    if not untrusted_workspace.is_dir():
        raise SecurityError("Evaluator workspace must be a directory")
    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_within(trusted_bundle, untrusted_workspace):
        raise SecurityError("Trusted evaluator bundle must be outside the learner workspace")
    if _is_within(trusted_key, untrusted_workspace):
        raise SecurityError("Evaluator verification key must be outside the learner workspace")
    if _is_within(destination, untrusted_workspace):
        raise SecurityError("Evidence output must be outside the learner workspace")
    envelope = read_provisioned_envelope(
        trusted_bundle,
        verification_key_path=trusted_key,
    )
    if (
        envelope.assignment_id != assignment_id
        or envelope.branch != branch
        or envelope.commit_sha != commit_sha
    ):
        raise SecurityError(
            "Trusted evaluator envelope does not match assignment, branch, and commit"
        )
    _verify_workspace_binding(untrusted_workspace, envelope)
    try:
        trusted_bundle.unlink()
        trusted_key.unlink()
    except OSError as exc:
        raise SecurityError("Trusted evaluator envelope could not be consumed") from exc
    evidence = CredentialFreeEvaluator().evaluate(
        bundle=envelope.bundle,
        assignment_id=assignment_id,
        commit_sha=commit_sha,
        workspace=untrusted_workspace,
        evaluator_binding=envelope.binding_digest,
        evaluator_key_id=envelope.key_id,
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
        evaluator_binding: str | None = None,
        evaluator_key_id: str | None = None,
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
                evaluator_binding=evaluator_binding,
                evaluator_key_id=evaluator_key_id,
                artifact_digest="sha256:" + "0" * 64,
            )
            return evidence.with_computed_digest()

    @staticmethod
    def _copy_submission(bundle: AssignmentBundle, workspace: Path, root: Path) -> None:
        total = 0
        for item in bundle.files:
            if item.role not in {"instructions", "starter", "public_test"}:
                continue
            content = _read_workspace_file(
                workspace,
                item.path,
                maximum=MAX_SUBMISSION_BYTES - total,
                label="Submission file",
            )
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


def _verify_workspace_binding(
    workspace: Path,
    envelope: TrustedBundleEnvelope,
) -> None:
    try:
        raw = _read_workspace_file(
            workspace,
            ".adaptive-tutor/assignment.json",
            maximum=MAX_ASSIGNMENT_METADATA_BYTES,
            label="Assignment metadata",
        )
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SecurityError("Assignment metadata is invalid") from exc
    if not isinstance(manifest, dict):
        raise SecurityError("Assignment metadata must be a JSON object")
    expected = {
        "schema_version": "1.0",
        "id": envelope.assignment_id,
        "branch": envelope.branch,
        "evaluator_binding": envelope.binding_digest,
        "evaluator_key_id": envelope.key_id,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise SecurityError("Assignment metadata does not match the trusted evaluator envelope")


def _read_workspace_file(root: Path, relative: str, *, maximum: int, label: str) -> bytes:
    """Read one regular file without following learner-controlled path symlinks."""
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise SecurityError(f"{label} has an unsafe path: {relative}")
    descriptors: list[int] = []
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    try:
        descriptors.append(os.open(root, os.O_RDONLY | directory | nofollow))
        for part in parts[:-1]:
            descriptors.append(
                os.open(
                    part,
                    os.O_RDONLY | directory | nofollow,
                    dir_fd=descriptors[-1],
                )
            )
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY | nofollow,
            dir_fd=descriptors[-1],
        )
        descriptors.append(file_descriptor)
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SecurityError(f"{label} must be one regular non-linked file: {relative}")
        if info.st_size > maximum:
            raise SecurityError(f"{label} exceeds the evaluator size limit")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(file_descriptor, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        raise SecurityError(f"{label} exceeds the evaluator size limit")
    except FileNotFoundError as exc:
        raise SecurityError(f"{label} is missing: {relative}") from exc
    except OSError as exc:
        raise SecurityError(f"{label} is not safely readable: {relative}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
