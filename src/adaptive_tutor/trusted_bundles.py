"""Tutor-host private bundles and signed public evaluator manifests."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, field_validator, model_validator

from .errors import SecurityError
from .models import AssignmentBundle, StrictModel
from .security import sha256_digest
from .time import parse_time, utc_now

MAX_ENVELOPE_BYTES = 6 * 1024 * 1024
_ASSIGNMENT_ID = re.compile(r"A-\d{4,12}")
_BRANCH = re.compile(r"assignment/\d{4,12}-[a-z0-9][a-z0-9-]{2,100}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SIGNATURE = re.compile(r"ed25519:[0-9a-f]{128}")


class PublicEvaluatorFile(StrictModel):
    """One learner-visible file whose original bytes are signed."""

    path: str = Field(pattern=r"^[A-Za-z0-9_.\-/]+$")
    digest: str = Field(pattern=_DIGEST.pattern)

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise ValueError("public evaluator paths must be normalized and relative")
        return value


class PublicEvaluatorLimits(StrictModel):
    """Resource limits enforced by the credential-free evaluator kit."""

    timeout_seconds: int = Field(default=90, ge=5, le=300)
    memory_mb: int = Field(default=768, ge=128, le=2048)
    pids: int = Field(default=64, ge=8, le=256)
    max_output_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024)


class PublicEvaluatorManifest(StrictModel):
    """Signed learner-visible evaluator contract with no private grading material."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: str = Field(pattern=_ASSIGNMENT_ID.pattern)
    branch: str = Field(pattern=_BRANCH.pattern)
    issued_at: str = Field(min_length=20, max_length=40)
    allowed_submissions: list[PublicEvaluatorFile] = Field(min_length=1, max_length=64)
    public_tests: list[PublicEvaluatorFile] = Field(default_factory=list, max_length=64)
    command: Literal["python-pytest-v1", "submission-policy-v1"]
    limits: PublicEvaluatorLimits = Field(default_factory=PublicEvaluatorLimits)
    evaluator_kit_digest: str = Field(pattern=_DIGEST.pattern)
    key_id: str = Field(pattern=r"[0-9a-f]{16}")
    signature: str = Field(pattern=_SIGNATURE.pattern)

    @model_validator(mode="after")
    def paths_are_disjoint_and_unique(self) -> PublicEvaluatorManifest:
        submissions = [item.path for item in self.allowed_submissions]
        tests = [item.path for item in self.public_tests]
        if len(submissions) != len(set(submissions)) or len(tests) != len(set(tests)):
            raise ValueError("public evaluator paths must be unique")
        if set(submissions) & set(tests):
            raise ValueError("submission and public-test paths must be disjoint")
        if self.command == "python-pytest-v1" and not tests:
            raise ValueError("the pytest evaluator requires public tests")
        if self.command == "submission-policy-v1" and tests:
            raise ValueError("the submission policy evaluator cannot include test files")
        return self


class TrustedBundleEnvelope(StrictModel):
    """Signed private assignment bundle retained only on the tutor host."""

    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["spool"] = "spool"
    assignment_id: str = Field(pattern=_ASSIGNMENT_ID.pattern)
    branch: str = Field(pattern=_BRANCH.pattern)
    issued_at: str = Field(min_length=20, max_length=40)
    key_id: str = Field(pattern=r"[0-9a-f]{16}")
    bundle_digest: str = Field(pattern=_DIGEST.pattern)
    binding_digest: str = Field(pattern=_DIGEST.pattern)
    bundle: AssignmentBundle
    signature: str = Field(pattern=_SIGNATURE.pattern)


def assignment_bundle_digest(bundle: AssignmentBundle) -> str:
    return sha256_digest(_canonical_json(bundle.model_dump(mode="json")))


def assignment_binding_digest(assignment_id: str, branch: str, bundle_digest: str) -> str:
    return sha256_digest(
        _canonical_json(
            {
                "schema_version": "1.0",
                "assignment_id": assignment_id,
                "branch": branch,
                "bundle_digest": bundle_digest,
            }
        )
    )


def public_manifest_digest(manifest: PublicEvaluatorManifest) -> str:
    """Return the stable digest used to bind dispatches and Actions evidence."""
    return sha256_digest(_canonical_json(manifest.model_dump(mode="json")))


def serialize_public_manifest(manifest: PublicEvaluatorManifest) -> str:
    return json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def public_verification_key_text(public_key: Ed25519PublicKey) -> str:
    return "ed25519:" + _public_key_bytes(public_key).hex() + "\n"


def verify_public_manifest(
    manifest: PublicEvaluatorManifest,
    *,
    verification_key: str,
    expected_assignment_id: str | None = None,
    expected_branch: str | None = None,
    expected_kit_digest: str | None = None,
) -> None:
    """Verify a public manifest signature and its externally supplied bindings."""
    public_key = _parse_public_verification_key(verification_key)
    key_id = hashlib.sha256(_public_key_bytes(public_key)).hexdigest()[:16]
    if not hmac.compare_digest(manifest.key_id, key_id):
        raise SecurityError("Public evaluator manifest uses an unknown verification key")
    if expected_assignment_id is not None and manifest.assignment_id != expected_assignment_id:
        raise SecurityError("Public evaluator manifest assignment does not match the dispatch")
    if expected_branch is not None and manifest.branch != expected_branch:
        raise SecurityError("Public evaluator manifest branch does not match the dispatch")
    if expected_kit_digest is not None and not hmac.compare_digest(
        manifest.evaluator_kit_digest, expected_kit_digest
    ):
        raise SecurityError("Public evaluator manifest kit does not match the trusted runtime")
    _validate_public_identity(manifest.assignment_id, manifest.branch)
    issued = parse_time(manifest.issued_at)
    if issued is None or issued > utc_now() + timedelta(minutes=5):
        raise SecurityError("Public evaluator manifest has an invalid issue time")
    try:
        signature = bytes.fromhex(manifest.signature.removeprefix("ed25519:"))
        public_key.verify(
            signature,
            _canonical_json(manifest.model_dump(mode="json", exclude={"signature"})),
        )
    except (InvalidSignature, ValueError) as exc:
        raise SecurityError("Public evaluator manifest signature verification failed") from exc


class TrustedBundleStore:
    """Owner-only private bundle store and public-manifest signer."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.root = self.data_dir / "trusted-evaluators"
        self.spool = self.root / "spool"
        self.key_path = self.root / "signing.key"
        self.public_key_path = self.root / "signing.pub"

    def seal(
        self,
        *,
        assignment_id: str,
        branch: str,
        bundle: AssignmentBundle,
    ) -> TrustedBundleEnvelope:
        """Sign and durably spool a bundle before its learner branch is published."""
        _validate_identity(assignment_id, branch, bundle)
        self._prepare()
        private_key, _ = self._key_pair()
        target = self.path_for(assignment_id)
        if target.exists() or target.is_symlink():
            existing = self.load(assignment_id)
            _require_expected(existing, assignment_id, branch, bundle)
            return existing

        envelope = _signed_envelope(
            private_key=private_key,
            assignment_id=assignment_id,
            branch=branch,
            bundle=bundle,
        )
        _write_once_private(target, _serialized(envelope))
        stored = self.load(assignment_id)
        _require_expected(stored, assignment_id, branch, bundle)
        return stored

    def public_manifest(
        self,
        *,
        assignment_id: str,
        branch: str,
        bundle: AssignmentBundle,
        evaluator_kit_digest: str,
    ) -> PublicEvaluatorManifest:
        """Derive a signed, learner-visible contract without private bundle fields."""
        _validate_identity(assignment_id, branch, bundle)
        if _DIGEST.fullmatch(evaluator_kit_digest) is None:
            raise SecurityError("Evaluator kit digest is invalid")
        self._prepare()
        private_key, _ = self._key_pair()
        return _signed_public_manifest(
            private_key=private_key,
            assignment_id=assignment_id,
            branch=branch,
            bundle=bundle,
            evaluator_kit_digest=evaluator_kit_digest,
        )

    def public_verification_key(self) -> str:
        self._prepare()
        _, public_key = self._key_pair()
        return public_verification_key_text(public_key)

    def load(self, assignment_id: str) -> TrustedBundleEnvelope:
        _validate_assignment_id(assignment_id)
        self._prepare(create_key=False)
        _, public_key = self._key_pair(create=False)
        envelope = _read_envelope(self.path_for(assignment_id))
        _verify_envelope(envelope, public_key)
        return envelope

    def path_for(self, assignment_id: str) -> Path:
        _validate_assignment_id(assignment_id)
        return self.spool / f"{assignment_id}.json"

    def _prepare(self, *, create_key: bool = True) -> None:
        _ensure_private_directory(self.data_dir)
        _ensure_private_directory(self.root)
        _ensure_private_directory(self.spool)
        if create_key:
            self._key_pair()

    def _key_pair(
        self,
        *,
        create: bool = True,
    ) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
        if not self.key_path.exists() and not self.key_path.is_symlink():
            if not create:
                raise SecurityError("Trusted evaluator signing key is missing")
            generated = Ed25519PrivateKey.generate()
            _write_once_private(self.key_path, _private_key_bytes(generated))
        private_bytes = _read_private_bytes(
            self.key_path,
            "Trusted evaluator signing key",
            maximum=32,
        )
        if len(private_bytes) != 32:
            raise SecurityError("Trusted evaluator signing key has an invalid length")
        try:
            private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        except ValueError as exc:  # pragma: no cover - length check is authoritative
            raise SecurityError("Trusted evaluator signing key is invalid") from exc
        public_key = private_key.public_key()
        expected_public = _public_key_bytes(public_key)
        if not self.public_key_path.exists() and not self.public_key_path.is_symlink():
            if not create:
                raise SecurityError("Trusted evaluator verification key is missing")
            _write_once_private(self.public_key_path, expected_public)
        stored_public = _read_private_bytes(
            self.public_key_path,
            "Trusted evaluator verification key",
            maximum=32,
        )
        if not hmac.compare_digest(stored_public, expected_public):
            raise SecurityError("Trusted evaluator key pair does not match")
        return private_key, public_key


def _read_envelope(path: Path) -> TrustedBundleEnvelope:
    try:
        payload = _read_private_bytes(
            path,
            "Trusted evaluator envelope",
            maximum=MAX_ENVELOPE_BYTES,
        )
        return TrustedBundleEnvelope.model_validate_json(payload)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SecurityError(f"Trusted evaluator envelope is invalid: {exc}") from exc


def _verify_envelope(
    envelope: TrustedBundleEnvelope,
    public_key: Ed25519PublicKey,
) -> None:
    digest = assignment_bundle_digest(envelope.bundle)
    if not hmac.compare_digest(envelope.bundle_digest, digest):
        raise SecurityError("Trusted evaluator bundle digest verification failed")
    binding = assignment_binding_digest(envelope.assignment_id, envelope.branch, digest)
    if not hmac.compare_digest(envelope.binding_digest, binding):
        raise SecurityError("Trusted evaluator assignment binding verification failed")
    key_id = hashlib.sha256(_public_key_bytes(public_key)).hexdigest()[:16]
    if not hmac.compare_digest(envelope.key_id, key_id):
        raise SecurityError("Trusted evaluator envelope uses an unknown verification key")
    try:
        signature = bytes.fromhex(envelope.signature.removeprefix("ed25519:"))
        public_key.verify(
            signature,
            _canonical_json(envelope.model_dump(mode="json", exclude={"signature"})),
        )
    except (InvalidSignature, ValueError) as exc:
        raise SecurityError("Trusted evaluator envelope signature verification failed") from exc
    _validate_identity(envelope.assignment_id, envelope.branch, envelope.bundle)


def _signed_envelope(
    *,
    private_key: Ed25519PrivateKey,
    assignment_id: str,
    branch: str,
    bundle: AssignmentBundle,
    issued_at: datetime | None = None,
) -> TrustedBundleEnvelope:
    digest = assignment_bundle_digest(bundle)
    issued = issued_at or utc_now()
    public_key = private_key.public_key()
    unsigned: dict[str, Any] = {
        "schema_version": "1.0",
        "purpose": "spool",
        "assignment_id": assignment_id,
        "branch": branch,
        "issued_at": issued.astimezone(UTC).isoformat(timespec="seconds"),
        "key_id": hashlib.sha256(_public_key_bytes(public_key)).hexdigest()[:16],
        "bundle_digest": digest,
        "binding_digest": assignment_binding_digest(assignment_id, branch, digest),
        "bundle": bundle.model_dump(mode="json"),
    }
    signature = private_key.sign(_canonical_json(unsigned)).hex()
    return TrustedBundleEnvelope.model_validate({**unsigned, "signature": "ed25519:" + signature})


def _signed_public_manifest(
    *,
    private_key: Ed25519PrivateKey,
    assignment_id: str,
    branch: str,
    bundle: AssignmentBundle,
    evaluator_kit_digest: str,
) -> PublicEvaluatorManifest:
    if bundle.validation_command == ["python", "-m", "pytest", "-q"]:
        command = "python-pytest-v1"
    elif not bundle.validation_command:
        command = "submission-policy-v1"
    else:
        raise SecurityError("Assignment uses an unsupported public evaluator command")
    allowed = [
        PublicEvaluatorFile(path=item.path, digest=sha256_digest(item.content))
        for item in bundle.files
        if item.role == "starter"
    ]
    public_tests = [
        PublicEvaluatorFile(path=item.path, digest=sha256_digest(item.content))
        for item in bundle.files
        if item.role == "public_test"
    ]
    public_key = private_key.public_key()
    unsigned: dict[str, Any] = {
        "schema_version": "1.0",
        "assignment_id": assignment_id,
        "branch": branch,
        "issued_at": utc_now().astimezone(UTC).isoformat(timespec="seconds"),
        "allowed_submissions": [item.model_dump(mode="json") for item in allowed],
        "public_tests": [item.model_dump(mode="json") for item in public_tests],
        "command": command,
        "limits": PublicEvaluatorLimits().model_dump(mode="json"),
        "evaluator_kit_digest": evaluator_kit_digest,
        "key_id": hashlib.sha256(_public_key_bytes(public_key)).hexdigest()[:16],
    }
    signature = private_key.sign(_canonical_json(unsigned)).hex()
    return PublicEvaluatorManifest.model_validate({**unsigned, "signature": "ed25519:" + signature})


def _parse_public_verification_key(value: str) -> Ed25519PublicKey:
    normalized = value.strip()
    if re.fullmatch(r"ed25519:[0-9a-f]{64}", normalized) is None:
        raise SecurityError("Public evaluator verification key is invalid")
    try:
        return Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(normalized.removeprefix("ed25519:"))
        )
    except ValueError as exc:  # pragma: no cover - regex validates the byte length
        raise SecurityError("Public evaluator verification key is invalid") from exc


def _require_expected(
    envelope: TrustedBundleEnvelope,
    assignment_id: str,
    branch: str,
    bundle: AssignmentBundle,
) -> None:
    if (
        envelope.assignment_id != assignment_id
        or envelope.branch != branch
        or envelope.bundle_digest != assignment_bundle_digest(bundle)
    ):
        raise SecurityError("Existing trusted evaluator envelope conflicts with the assignment")


def _validate_identity(assignment_id: str, branch: str, bundle: AssignmentBundle) -> None:
    _validate_assignment_id(assignment_id)
    if _BRANCH.fullmatch(branch) is None:
        raise SecurityError("Invalid assignment branch for trusted evaluator envelope")
    counter = assignment_id.removeprefix("A-")
    expected = f"assignment/{counter}-{bundle.slug}"
    if branch != expected:
        raise SecurityError("Assignment branch does not match the evaluator bundle identity")


def _validate_public_identity(assignment_id: str, branch: str) -> None:
    _validate_assignment_id(assignment_id)
    match = _BRANCH.fullmatch(branch)
    if match is None or branch.split("/", 1)[1].split("-", 1)[0] != assignment_id[2:]:
        raise SecurityError("Public evaluator branch does not match the assignment identity")


def _validate_assignment_id(assignment_id: str) -> None:
    if _ASSIGNMENT_ID.fullmatch(assignment_id) is None:
        raise SecurityError("Invalid assignment identifier for trusted evaluator envelope")


def _serialized(envelope: TrustedBundleEnvelope) -> bytes:
    return (envelope.model_dump_json(indent=2) + "\n").encode()


def _private_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_key_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _ensure_private_directory(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        path.mkdir(parents=True, mode=0o700)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SecurityError(f"Private evaluator path is not a directory: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SecurityError(f"Private evaluator directory is accessible to other users: {path}")
    _assert_owner(info.st_uid, path)


def _assert_private_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SecurityError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SecurityError(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        raise SecurityError(f"{label} must not have additional hard links")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SecurityError(f"{label} must be owner-readable only")
    _assert_owner(info.st_uid, path)


def _read_private_bytes(path: Path, label: str, *, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise SecurityError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise SecurityError(f"{label} must be a regular non-symlink file") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SecurityError(f"{label} must be a regular non-symlink file")
        if info.st_nlink != 1:
            raise SecurityError(f"{label} must not have additional hard links")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise SecurityError(f"{label} must be owner-readable only")
        _assert_owner(info.st_uid, path)
        if info.st_size > maximum:
            raise SecurityError(f"{label} exceeds the size limit")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
        raise SecurityError(f"{label} exceeds the size limit")
    finally:
        os.close(descriptor)


def _assert_owner(owner: int, path: Path) -> None:
    if hasattr(os, "geteuid") and owner != os.geteuid():
        raise SecurityError(f"Private evaluator path is owned by another user: {path}")


def _write_once_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        raise SecurityError(f"Could not create private evaluator file: {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    _assert_private_regular_file(path, "Private evaluator file")
