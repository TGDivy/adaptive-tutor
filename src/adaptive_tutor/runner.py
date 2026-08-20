"""Credential-free deterministic evaluator for ephemeral CI runners."""

from __future__ import annotations

import hashlib
import json
import os
import resource
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

from .errors import SecurityError
from .models import AutomatedCheck, AutomatedEvaluation
from .security import assert_credentials_absent, sha256_digest, untrusted_process_environment
from .trusted_bundles import (
    PublicEvaluatorLimits,
    PublicEvaluatorManifest,
    public_manifest_digest,
    verify_public_manifest,
)

MAX_SUBMISSION_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_ASSIGNMENT_METADATA_BYTES = 64 * 1024
MAX_SUPERVISOR_RECORD_BYTES = 16 * 1024
EVALUATION_TIMEOUT_SECONDS = 90
SANDBOX_ROOT = Path("/evaluation")
SANDBOX_PACKAGE_ROOT = Path("/runtime/adaptive-tutor")
SANDBOX_TMP = Path("/tmp")  # noqa: S108 - a private tmpfs inside the sandbox
SANDBOX_HOME = SANDBOX_TMP / "home"
PUBLIC_MANIFEST_PATH = ".adaptive-tutor/evaluator-manifest.json"
EVALUATOR_KIT_FILES = (
    "__init__.py",
    "_evaluator_supervisor.py",
    "errors.py",
    "models.py",
    "public_evaluator.py",
    "runner.py",
    "security.py",
    "time.py",
    "trusted_bundles.py",
)


@dataclass(frozen=True)
class _SupervisedResult:
    status: str
    summary: str


def evaluator_kit_digest() -> str:
    """Digest the exact installed sources that implement public evaluation."""
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in EVALUATOR_KIT_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((package_root / name).read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def evaluate_public_workspace_to_file(
    *,
    verification_key_path: Path,
    workspace: Path,
    output_path: Path,
    assignment_id: str,
    branch: str,
    commit_sha: str,
    dispatch_nonce: str,
    expected_manifest_digest: str,
    expected_evaluator_kit_digest: str,
    evaluator_ref: str,
    workflow_digest: str,
    workflow_commit: str,
    repository_id: int,
) -> AutomatedEvaluation:
    """Verify a signed public manifest and evaluate one hosted-runner checkout."""
    workspace_input = workspace.expanduser().absolute()
    if workspace_input.is_symlink():
        raise SecurityError("Evaluator workspace must not be a symlink")
    untrusted_workspace = workspace_input.resolve(strict=True)
    if not untrusted_workspace.is_dir():
        raise SecurityError("Evaluator workspace must be a directory")
    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_within(destination, untrusted_workspace):
        raise SecurityError("Evidence output must be outside the learner workspace")
    key_path = verification_key_path.expanduser().resolve(strict=True)
    if _is_within(key_path, untrusted_workspace):
        raise SecurityError("Evaluator verification key must be outside the learner workspace")
    try:
        key_info = key_path.stat()
        if not stat.S_ISREG(key_info.st_mode) or key_info.st_size > 256:
            raise SecurityError("Public evaluator verification key is not a bounded regular file")
        verification_key = key_path.read_text(encoding="ascii")
        manifest_payload = _read_workspace_file(
            untrusted_workspace,
            PUBLIC_MANIFEST_PATH,
            maximum=MAX_ASSIGNMENT_METADATA_BYTES,
            label="Public evaluator manifest",
        )
        manifest = PublicEvaluatorManifest.model_validate_json(manifest_payload)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SecurityError(f"Public evaluator control data is invalid: {exc}") from exc
    observed_manifest_digest = public_manifest_digest(manifest)
    if not secrets.compare_digest(observed_manifest_digest, expected_manifest_digest):
        raise SecurityError("Public evaluator manifest does not match the dispatched digest")
    observed_kit_digest = evaluator_kit_digest()
    if not secrets.compare_digest(observed_kit_digest, expected_evaluator_kit_digest):
        raise SecurityError("Public evaluator kit does not match the dispatched digest")
    verify_public_manifest(
        manifest,
        verification_key=verification_key,
        expected_assignment_id=assignment_id,
        expected_branch=branch,
        expected_kit_digest=observed_kit_digest,
    )
    evidence = PublicCredentialFreeEvaluator().evaluate(
        manifest=manifest,
        assignment_id=assignment_id,
        commit_sha=commit_sha,
        workspace=untrusted_workspace,
        dispatch_nonce=dispatch_nonce,
        manifest_digest=observed_manifest_digest,
        workflow_digest=workflow_digest,
        workflow_commit=workflow_commit,
        evaluator_ref=evaluator_ref,
        evaluator_kit_digest=observed_kit_digest,
        repository_id=repository_id,
    )
    _write_evidence(destination, evidence)
    return evidence


class PublicCredentialFreeEvaluator:
    """Run only signed learner-visible tests under the networkless sandbox."""

    def evaluate(
        self,
        *,
        manifest: PublicEvaluatorManifest,
        assignment_id: str,
        commit_sha: str,
        workspace: Path,
        dispatch_nonce: str,
        manifest_digest: str,
        workflow_digest: str,
        workflow_commit: str,
        evaluator_ref: str,
        evaluator_kit_digest: str,
        repository_id: int,
    ) -> AutomatedEvaluation:
        started = datetime.now(UTC)
        started_clock = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="adaptive-tutor-public-evaluation-") as temporary:
            root = Path(temporary)
            submission = root / "submission"
            public_tests = root / "trusted-tests" / "public"
            hidden_tests = root / "trusted-tests" / "hidden"
            submission.mkdir(parents=True)
            public_tests.mkdir(parents=True)
            hidden_tests.mkdir(parents=True)
            changed = False
            meaningful = False
            total = 0
            for item in manifest.allowed_submissions:
                content = _read_workspace_file(
                    workspace,
                    item.path,
                    maximum=MAX_SUBMISSION_BYTES - total,
                    label="Submission file",
                )
                total += len(content)
                if total > MAX_SUBMISSION_BYTES:
                    raise SecurityError("Submission exceeds the evaluator size limit")
                changed = changed or sha256_digest(content) != item.digest
                meaningful = meaningful or bool(content.strip())
                target = _safe_path(submission, item.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            for item in manifest.public_tests:
                content = _read_workspace_file(
                    workspace,
                    item.path,
                    maximum=MAX_SUBMISSION_BYTES,
                    label="Public test",
                )
                if not secrets.compare_digest(sha256_digest(content), item.digest):
                    raise SecurityError(f"Public test was modified: {item.path}")
                target = _safe_path(public_tests, item.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

            environment = untrusted_process_environment(SANDBOX_HOME)
            assert_credentials_absent(environment)
            if manifest.command == "python-pytest-v1":
                output_path = root / "evaluation-output.txt"
                supervised = _run_supervised_tests(
                    root,
                    output_path,
                    environment,
                    timeout_seconds=manifest.limits.timeout_seconds,
                    limits=manifest.limits,
                )
                sandbox_status = "pass" if supervised.status != "error" else "error"
                sandbox_summary = (
                    "Public tests ran in a read-only, networkless PID namespace"
                    if supervised.status != "error"
                    else supervised.summary
                )
            else:
                supervised = _SupervisedResult(
                    "pass" if changed and meaningful else "fail",
                    "Submission content changed and is non-empty"
                    if changed and meaningful
                    else "Submission content must be changed and non-empty",
                )
                sandbox_status = "skipped"
                sandbox_summary = "No learner code is executed for this assignment format"
            duration = int((time.monotonic() - started_clock) * 1000)
            checks = [
                AutomatedCheck(
                    name="signed public manifest",
                    status="pass",
                    category="policy",
                    summary="Assignment paths, tests, command, and limits are signature-verified",
                ),
                AutomatedCheck(
                    name="submission boundary",
                    status="pass",
                    category="policy",
                    summary="Only signed bounded regular files entered the evaluator",
                ),
                AutomatedCheck(
                    name="credential boundary",
                    status="pass",
                    category="policy",
                    summary="Evaluator environment contains no credential-like variables",
                ),
                AutomatedCheck(
                    name="ephemeral sandbox",
                    status=sandbox_status,  # type: ignore[arg-type]
                    category="policy",
                    summary=sandbox_summary,
                ),
                AutomatedCheck(
                    name=(
                        "public tests"
                        if manifest.command == "python-pytest-v1"
                        else "submission policy"
                    ),
                    status=supervised.status,  # type: ignore[arg-type]
                    category="test" if manifest.command == "python-pytest-v1" else "policy",
                    summary=supervised.summary,
                    duration_ms=duration,
                ),
            ]
            evidence = AutomatedEvaluation(
                assignment_id=assignment_id,
                commit_sha=commit_sha,
                checks=checks,
                started_at=started,
                completed_at=datetime.now(UTC),
                runner="adaptive-tutor-github-hosted",
                evaluator_key_id=manifest.key_id,
                dispatch_nonce=dispatch_nonce,
                manifest_digest=manifest_digest,
                workflow_digest=workflow_digest,
                workflow_commit=workflow_commit,
                evaluator_ref=evaluator_ref,
                evaluator_kit_digest=evaluator_kit_digest,
                repository_id=repository_id,
                artifact_digest="sha256:" + "0" * 64,
            )
            return evidence.with_computed_digest()


def _write_evidence(destination: Path, evidence: AutomatedEvaluation) -> None:
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


def _run_supervised_tests(
    root: Path,
    output_path: Path,
    environment: dict[str, str],
    *,
    timeout_seconds: int = EVALUATION_TIMEOUT_SECONDS,
    limits: PublicEvaluatorLimits | None = None,
) -> _SupervisedResult:
    bubblewrap = shutil.which("bwrap")
    if os.name != "posix" or bubblewrap is None:
        raise SecurityError("Bubblewrap is required for credential-free evaluator isolation")

    nonce = secrets.token_hex(32)
    read_fd, write_fd = os.pipe()
    nonce_read_fd, nonce_write_fd = os.pipe()
    os.write(nonce_write_fd, nonce.encode("ascii"))
    os.close(nonce_write_fd)
    command = _bubblewrap_command(
        bubblewrap=bubblewrap,
        root=root,
        status_fd=write_fd,
        nonce_fd=nonce_read_fd,
    )
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    try:
        with output_path.open("wb") as output:
            process = subprocess.Popen(  # noqa: S603 - fixed sandbox and supervisor command
                command,
                cwd=root,
                env=environment,
                stdout=output,
                stderr=subprocess.STDOUT,
                pass_fds=(write_fd, nonce_read_fd),
                start_new_session=True,
                preexec_fn=partial(_resource_limits, limits),
            )
            os.close(write_fd)
            write_fd = -1
            os.close(nonce_read_fd)
            nonce_read_fd = -1
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_group(process.pid)
                process.wait()
            finally:
                _kill_process_group(process.pid)
        record_bytes = _read_supervisor_record(read_fd)
    finally:
        if process is not None and process.poll() is None:
            _kill_process_group(process.pid)
            process.wait()
        if write_fd >= 0:
            os.close(write_fd)
        if nonce_read_fd >= 0:
            os.close(nonce_read_fd)
        os.close(read_fd)

    if timed_out:
        return _SupervisedResult(
            status="error",
            summary=f"Evaluation exceeded the {timeout_seconds} second limit",
        )
    if process is None or process.returncode != 0:
        return _SupervisedResult(
            status="error",
            summary="The isolated evaluator terminated without trusted completion",
        )
    return _interpret_supervisor_record(record_bytes, nonce)


def _bubblewrap_command(
    *,
    bubblewrap: str,
    root: Path,
    status_fd: int,
    nonce_fd: int,
) -> list[str]:
    package_root = Path(__file__).resolve().parents[1]
    python_path = [str(SANDBOX_PACKAGE_ROOT)]
    for path_name in ("purelib", "platlib"):
        configured = sysconfig.get_path(path_name)
        if configured and configured not in python_path:
            python_path.append(configured)
    python_path.append(str(SANDBOX_ROOT / "submission"))
    command = [
        bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--cap-drop",
        "ALL",
    ]
    for source, destination in _runtime_mounts(package_root):
        command.extend(("--ro-bind", str(source), str(destination)))
    command.extend(
        (
            "--ro-bind",
            str(root),
            str(SANDBOX_ROOT),
            "--tmpfs",
            str(SANDBOX_TMP),
            "--dev",
            "/dev",
            "--dir",
            str(SANDBOX_HOME),
            "--setenv",
            "HOME",
            str(SANDBOX_HOME),
            "--setenv",
            "TMPDIR",
            str(SANDBOX_TMP),
            "--setenv",
            "PYTHONPATH",
            os.pathsep.join(python_path),
            "--setenv",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "1",
            "--chdir",
            "/",
            str(Path(sys.executable).resolve()),
            "-m",
            "adaptive_tutor._evaluator_supervisor",
            "--status-fd",
            str(status_fd),
            "--nonce-fd",
            str(nonce_fd),
            "--workdir",
            str(SANDBOX_ROOT / "submission"),
            "--public-tests",
            str(SANDBOX_ROOT / "trusted-tests" / "public"),
            "--hidden-tests",
            str(SANDBOX_ROOT / "trusted-tests" / "hidden"),
        )
    )
    return command


def _runtime_mounts(package_root: Path) -> list[tuple[Path, Path]]:
    candidates: list[tuple[Path, Path]] = [
        (Path(sys.prefix), Path(sys.prefix)),
        (package_root, SANDBOX_PACKAGE_ROOT),
        (Path(sys.executable).resolve(), Path(sys.executable).resolve()),
    ]
    for configured in (
        sysconfig.get_path("stdlib"),
        sysconfig.get_path("platstdlib"),
        sysconfig.get_config_var("LIBDIR"),
        str(Path(sys.base_prefix) / "lib"),
        str(Path(sys.base_prefix) / "lib64"),
        "/lib",
        "/lib64",
        "/usr/lib",
        "/usr/lib64",
        "/etc/ld.so.cache",
    ):
        if configured:
            path = Path(configured)
            candidates.append((path, path))

    mounts: list[tuple[Path, Path]] = []
    seen_destinations: set[Path] = set()
    for source, destination in candidates:
        if not source.exists() or destination in seen_destinations:
            continue
        if source == destination and any(
            parent_destination != destination and parent_destination in destination.parents
            for _, parent_destination in mounts
        ):
            continue
        mounts.append((source, destination))
        seen_destinations.add(destination)
    return mounts


def _read_supervisor_record(file_descriptor: int) -> bytes:
    os.set_blocking(file_descriptor, False)
    content = bytearray()
    while len(content) <= MAX_SUPERVISOR_RECORD_BYTES:
        try:
            chunk = os.read(file_descriptor, MAX_SUPERVISOR_RECORD_BYTES + 1 - len(content))
        except BlockingIOError:
            break
        if not chunk:
            break
        content.extend(chunk)
    return bytes(content)


def _interpret_supervisor_record(payload: bytes, nonce: str) -> _SupervisedResult:
    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _SupervisedResult("error", "The evaluator completion record was missing or invalid")
    expected_fields = {
        "schema_version",
        "nonce",
        "exit_code",
        "collected",
        "passed",
        "failed",
        "skipped",
        "internal_errors",
        "worker_crashes",
        "supervisor_error",
    }
    if not isinstance(record, dict) or set(record) != expected_fields:
        return _SupervisedResult("error", "The evaluator completion record had an invalid schema")
    numeric_fields = (
        "exit_code",
        "collected",
        "passed",
        "failed",
        "skipped",
        "internal_errors",
        "worker_crashes",
    )
    if (
        record["schema_version"] != "1.0"
        or not secrets.compare_digest(str(record["nonce"]), nonce)
        or any(type(record[field]) is not int or record[field] < 0 for field in numeric_fields)
        or type(record["supervisor_error"]) is not bool
    ):
        return _SupervisedResult("error", "The evaluator completion record failed validation")

    collected = record["collected"]
    passed = record["passed"]
    failed = record["failed"]
    skipped = record["skipped"]
    reported = passed + failed + skipped
    if record["worker_crashes"] > 0 and not record["supervisor_error"]:
        return _SupervisedResult(
            "fail", "An isolated test worker terminated before completing its test set"
        )
    if (
        record["exit_code"] == 0
        and not record["supervisor_error"]
        and record["internal_errors"] == 0
        and collected > 0
        and reported == collected
        and passed == collected
    ):
        return _SupervisedResult("pass", f"{passed} isolated tests passed")
    if collected > 0 and reported <= collected and record["exit_code"] in {0, 1}:
        unsuccessful = collected - passed
        return _SupervisedResult(
            "fail",
            f"{passed} of {collected} isolated tests passed; {unsuccessful} did not pass",
        )
    return _SupervisedResult("error", "The isolated test harness did not complete normally")


def _kill_process_group(process_id: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process_id, signal.SIGKILL)


def _safe_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        raise SecurityError(f"Path escapes evaluator workspace: {relative}")
    return candidate


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


def _resource_limits(limits: PublicEvaluatorLimits | None = None) -> None:
    timeout = limits.timeout_seconds if limits is not None else 60
    output_bytes = limits.max_output_bytes if limits is not None else MAX_OUTPUT_BYTES
    memory_bytes = (limits.memory_mb * 1024**2) if limits is not None else 1024**3
    process_limit = limits.pids if limits is not None else 128
    resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if hasattr(resource, "RLIMIT_AS"):
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    if hasattr(resource, "RLIMIT_NPROC"):
        resource.setrlimit(resource.RLIMIT_NPROC, (process_limit, process_limit))
    if hasattr(resource, "RLIMIT_NOFILE"):
        maximum = min(256, resource.getrlimit(resource.RLIMIT_NOFILE)[1])
        resource.setrlimit(resource.RLIMIT_NOFILE, (maximum, maximum))
