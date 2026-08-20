from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.errors import SecurityError
from adaptive_tutor.generation import CurriculumAssignmentGenerator
from adaptive_tutor.models import AssignmentBundle, AssignmentRequest, LearnerContext
from adaptive_tutor.trusted_bundles import (
    PublicEvaluatorManifest,
    TrustedBundleStore,
    public_manifest_digest,
    serialize_public_manifest,
    verify_public_manifest,
)


def bundle() -> AssignmentBundle:
    package = CurriculumLoader().load(bundled_curriculum_path())
    request = AssignmentRequest(
        learner_id="bundle-test",
        curriculum_id=package.metadata.id,
        profile_id=package.metadata.default_profile,
        target_concepts=["programming.invariants"],
        target_difficulty=4,
        context=LearnerContext(),
    )
    return CurriculumAssignmentGenerator(package).generate(request)


def identity(value: AssignmentBundle) -> tuple[str, str]:
    return "A-0042", f"assignment/0042-{value.slug}"


def test_public_manifest_is_signed_redacted_and_bound_to_the_evaluator_kit(
    tmp_path: Path,
) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    kit_digest = "sha256:" + "a" * 64

    manifest = store.public_manifest(
        assignment_id=assignment_id,
        branch=branch,
        bundle=value,
        evaluator_kit_digest=kit_digest,
    )
    serialized = serialize_public_manifest(manifest)
    verify_public_manifest(
        PublicEvaluatorManifest.model_validate_json(serialized),
        verification_key=store.public_verification_key(),
        expected_assignment_id=assignment_id,
        expected_branch=branch,
        expected_kit_digest=kit_digest,
    )

    assert public_manifest_digest(manifest).startswith("sha256:")
    assert manifest.command == "python-pytest-v1"
    assert {item.path for item in manifest.allowed_submissions}
    assert {item.path for item in manifest.public_tests}
    for forbidden in (
        "hidden_evaluator",
        "reference_expectations",
        '"rubric"',
        "reference_replacements",
        "extra_tests",
    ):
        assert forbidden not in serialized
    private_contents = {
        item.content for item in value.files if item.role in {"reference", "evaluator"}
    }
    assert all(content not in serialized for content in private_contents)


def test_public_manifest_rejects_tampering_and_wrong_runtime(tmp_path: Path) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    manifest = store.public_manifest(
        assignment_id=assignment_id,
        branch=branch,
        bundle=value,
        evaluator_kit_digest="sha256:" + "b" * 64,
    )
    tampered = manifest.model_copy(update={"command": "submission-policy-v1"})

    with pytest.raises(SecurityError, match="signature verification failed"):
        verify_public_manifest(tampered, verification_key=store.public_verification_key())
    with pytest.raises(SecurityError, match="trusted runtime"):
        verify_public_manifest(
            manifest,
            verification_key=store.public_verification_key(),
            expected_kit_digest="sha256:" + "c" * 64,
        )


def test_private_bundle_is_signed_spooled_and_owner_only(tmp_path: Path) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    sealed = store.seal(assignment_id=assignment_id, branch=branch, bundle=value)

    assert store.load(assignment_id) == sealed
    assert sealed.purpose == "spool"
    assert sealed.bundle == value
    assert sealed.bundle_digest.startswith("sha256:")
    assert sealed.binding_digest.startswith("sha256:")
    assert sealed.signature.startswith("ed25519:")
    for path, mode in (
        (store.root, 0o700),
        (store.spool, 0o700),
        (store.key_path, 0o600),
        (store.public_key_path, 0o600),
        (store.path_for(assignment_id), 0o600),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_private_spool_is_idempotent_but_rejects_assignment_conflicts(
    tmp_path: Path,
) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    first = store.seal(assignment_id=assignment_id, branch=branch, bundle=value)

    assert store.seal(assignment_id=assignment_id, branch=branch, bundle=value) == first
    changed = value.model_copy(update={"title": "A conflicting trusted assignment title"})
    with pytest.raises(SecurityError, match="conflicts"):
        store.seal(assignment_id=assignment_id, branch=branch, bundle=changed)


@pytest.mark.parametrize("field", ["bundle", "signature"])
def test_tampered_private_spool_fails_closed(tmp_path: Path, field: str) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    store.seal(assignment_id=assignment_id, branch=branch, bundle=value)
    path = store.path_for(assignment_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if field == "bundle":
        payload["bundle"]["title"] = "Tampered evaluator bundle"
    else:
        payload["signature"] = "ed25519:" + "0" * 128
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(SecurityError, match=r"digest|signature"):
        store.load(assignment_id)


def test_private_spool_rejects_hard_links_and_open_permissions(tmp_path: Path) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    store.seal(assignment_id=assignment_id, branch=branch, bundle=value)
    path = store.path_for(assignment_id)

    alias = tmp_path / "bundle-hardlink.json"
    os.link(path, alias)
    with pytest.raises(SecurityError, match="hard links"):
        store.load(assignment_id)
    alias.unlink()

    path.chmod(0o644)
    with pytest.raises(SecurityError, match="owner-readable only"):
        store.load(assignment_id)
