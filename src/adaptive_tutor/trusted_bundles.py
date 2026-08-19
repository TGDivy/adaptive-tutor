"""Authenticated storage and provisioning for hidden evaluator bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, model_validator

from .errors import SecurityError
from .models import AssignmentBundle, StrictModel
from .security import sha256_digest
from .time import parse_time, utc_now

MAX_ENVELOPE_BYTES = 6 * 1024 * 1024
MAX_STAGE_TTL_SECONDS = 3600
DEFAULT_STAGE_TTL_SECONDS = 1800
_ASSIGNMENT_ID = re.compile(r"A-\d{4,12}")
_BRANCH = re.compile(r"assignment/\d{4,12}-[a-z0-9][a-z0-9-]{2,100}")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40,64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SIGNATURE = re.compile(r"ed25519:[0-9a-f]{128}")


class TrustedBundleEnvelope(StrictModel):
    """Signed assignment binding retained in the spool or staged for one runner."""

    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["spool", "runner"]
    assignment_id: str = Field(pattern=_ASSIGNMENT_ID.pattern)
    branch: str = Field(pattern=_BRANCH.pattern)
    commit_sha: str | None = Field(default=None, pattern=_COMMIT_SHA.pattern)
    issued_at: str = Field(min_length=20, max_length=40)
    expires_at: str | None = Field(default=None, min_length=20, max_length=40)
    key_id: str = Field(pattern=r"[0-9a-f]{16}")
    bundle_digest: str = Field(pattern=_DIGEST.pattern)
    binding_digest: str = Field(pattern=_DIGEST.pattern)
    bundle: AssignmentBundle
    signature: str = Field(pattern=_SIGNATURE.pattern)

    @model_validator(mode="after")
    def purpose_fields_are_coherent(self) -> TrustedBundleEnvelope:
        if self.purpose == "spool" and (self.commit_sha is not None or self.expires_at is not None):
            raise ValueError("spool envelopes cannot be commit-bound or expiring")
        if self.purpose == "runner" and (
            self.commit_sha is None or self.expires_at is None
        ):
            raise ValueError("runner envelopes require a commit and expiration")
        return self


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


class TrustedBundleStore:
    """Owner-only signed spool used by trusted ephemeral-runner provisioners."""

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
            purpose="spool",
            assignment_id=assignment_id,
            branch=branch,
            bundle=bundle,
        )
        _write_once_private(target, _serialized(envelope))
        stored = self.load(assignment_id)
        _require_expected(stored, assignment_id, branch, bundle)
        return stored

    def load(self, assignment_id: str) -> TrustedBundleEnvelope:
        _validate_assignment_id(assignment_id)
        self._prepare(create_key=False)
        _, public_key = self._key_pair(create=False)
        envelope = _read_envelope(self.path_for(assignment_id))
        _verify_envelope(envelope, public_key, expected_purpose="spool")
        return envelope

    def stage(
        self,
        *,
        assignment_id: str,
        branch: str,
        commit_sha: str,
        destination: Path,
        verification_key_destination: Path,
        ttl_seconds: int = DEFAULT_STAGE_TTL_SECONDS,
    ) -> TrustedBundleEnvelope:
        """Verify the spool and issue one short-lived, commit-bound runner envelope."""
        if _COMMIT_SHA.fullmatch(commit_sha) is None:
            raise SecurityError("Invalid commit SHA for evaluator staging")
        if not 1 <= ttl_seconds <= MAX_STAGE_TTL_SECONDS:
            raise SecurityError("Evaluator staging TTL is outside the allowed range")
        sealed = self.load(assignment_id)
        if sealed.assignment_id != assignment_id or sealed.branch != branch:
            raise SecurityError("Trusted evaluator envelope does not match assignment and branch")
        private_key, public_key = self._key_pair(create=False)
        now = utc_now()
        staged = _signed_envelope(
            private_key=private_key,
            purpose="runner",
            assignment_id=assignment_id,
            branch=branch,
            bundle=sealed.bundle,
            commit_sha=commit_sha,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        envelope_path = _staging_path(destination, self.root, "Evaluator envelope")
        public_key_path = _staging_path(
            verification_key_destination,
            self.root,
            "Evaluator verification key",
        )
        if envelope_path == public_key_path:
            raise SecurityError("Evaluator envelope and verification key need separate paths")
        _ensure_private_directory(envelope_path.parent)
        _ensure_private_directory(public_key_path.parent)
        _replace_private(public_key_path, _public_key_bytes(public_key))
        _replace_private(envelope_path, _serialized(staged))
        verified = read_provisioned_envelope(
            envelope_path,
            verification_key_path=public_key_path,
            now=now,
        )
        if verified != staged:
            raise SecurityError("Staged evaluator envelope failed verification")
        return verified

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


def read_provisioned_envelope(
    path: Path,
    *,
    verification_key_path: Path,
    now: datetime | None = None,
) -> TrustedBundleEnvelope:
    """Authenticate a short-lived envelope provisioned for one runner job."""
    envelope_path = path.expanduser().absolute()
    public_key_path = verification_key_path.expanduser().absolute()
    key_bytes = _read_private_bytes(
        public_key_path,
        "Trusted evaluator verification key",
        maximum=32,
    )
    if len(key_bytes) != 32:
        raise SecurityError("Trusted evaluator verification key has an invalid length")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(key_bytes)
    except ValueError as exc:  # pragma: no cover - length check is authoritative
        raise SecurityError("Trusted evaluator verification key is invalid") from exc
    envelope = _read_envelope(envelope_path)
    _verify_envelope(
        envelope,
        public_key,
        expected_purpose="runner",
        now=now or utc_now(),
    )
    return envelope


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
    *,
    expected_purpose: Literal["spool", "runner"],
    now: datetime | None = None,
) -> None:
    if envelope.purpose != expected_purpose:
        raise SecurityError("Trusted evaluator envelope has the wrong purpose")
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
    if expected_purpose == "runner":
        current = now or utc_now()
        issued = parse_time(envelope.issued_at)
        expires = parse_time(envelope.expires_at)
        if issued is None or expires is None or expires <= issued:
            raise SecurityError("Trusted evaluator envelope has invalid validity timestamps")
        if issued > current + timedelta(minutes=5):
            raise SecurityError("Trusted evaluator envelope was issued in the future")
        if expires > issued + timedelta(seconds=MAX_STAGE_TTL_SECONDS):
            raise SecurityError("Trusted evaluator envelope validity is too long")
        if current > expires:
            raise SecurityError("Trusted evaluator envelope has expired")


def _signed_envelope(
    *,
    private_key: Ed25519PrivateKey,
    purpose: Literal["spool", "runner"],
    assignment_id: str,
    branch: str,
    bundle: AssignmentBundle,
    commit_sha: str | None = None,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> TrustedBundleEnvelope:
    digest = assignment_bundle_digest(bundle)
    issued = issued_at or utc_now()
    public_key = private_key.public_key()
    unsigned: dict[str, Any] = {
        "schema_version": "1.0",
        "purpose": purpose,
        "assignment_id": assignment_id,
        "branch": branch,
        "commit_sha": commit_sha,
        "issued_at": issued.astimezone(UTC).isoformat(timespec="seconds"),
        "expires_at": expires_at.astimezone(UTC).isoformat(timespec="seconds")
        if expires_at is not None
        else None,
        "key_id": hashlib.sha256(_public_key_bytes(public_key)).hexdigest()[:16],
        "bundle_digest": digest,
        "binding_digest": assignment_binding_digest(assignment_id, branch, digest),
        "bundle": bundle.model_dump(mode="json"),
    }
    signature = private_key.sign(_canonical_json(unsigned)).hex()
    return TrustedBundleEnvelope.model_validate(
        {**unsigned, "signature": "ed25519:" + signature}
    )


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


def _staging_path(path: Path, spool_root: Path, label: str) -> Path:
    target = path.expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.absolute()
    if _is_within(target, spool_root):
        raise SecurityError(f"{label} staging destination must be outside the private spool")
    if target.exists() or target.is_symlink():
        _assert_private_regular_file(target, f"{label} staging destination")
    return target


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


def _replace_private(path: Path, payload: bytes) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".adaptive-tutor-envelope-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary.name, 0o600)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        _assert_private_regular_file(path, "Evaluator staging destination")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _is_within(path: Path, root: Path) -> bool:
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    return resolved_path == resolved_root or resolved_root in resolved_path.parents
