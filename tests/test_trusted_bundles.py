from __future__ import annotations

import json
import os
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from adaptive_tutor.assignments import AssignmentService, AssignmentValidator
from adaptive_tutor.cli import app
from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.db import Database
from adaptive_tutor.errors import SecurityError
from adaptive_tutor.generation import CurriculumAssignmentGenerator
from adaptive_tutor.models import (
    AssignmentBundle,
    AssignmentRequest,
    CurriculumPackage,
    LearnerContext,
)
from adaptive_tutor.trusted_bundles import (
    TrustedBundleStore,
    assignment_binding_digest,
    assignment_bundle_digest,
    read_provisioned_envelope,
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


def test_bundle_is_signed_spooled_and_staged_with_owner_only_permissions(
    tmp_path: Path,
) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")

    sealed = store.seal(assignment_id=assignment_id, branch=branch, bundle=value)
    envelope_path = store.path_for(assignment_id)
    staged_path = tmp_path / "runner" / "trusted" / "assignment-bundle.json"
    staged_key = tmp_path / "runner" / "trusted" / "evaluator-signing.pub"
    staged = store.stage(
        assignment_id=assignment_id,
        branch=branch,
        commit_sha="a" * 40,
        destination=staged_path,
        verification_key_destination=staged_key,
    )

    assert staged.bundle == sealed.bundle
    assert staged.purpose == "runner"
    assert staged.commit_sha == "a" * 40
    assert read_provisioned_envelope(
        staged_path, verification_key_path=staged_key
    ) == staged
    assert sealed.bundle_digest.startswith("sha256:")
    assert sealed.binding_digest.startswith("sha256:")
    assert sealed.signature.startswith("ed25519:")
    for path, mode in (
        (store.root, 0o700),
        (store.spool, 0o700),
        (store.key_path, 0o600),
        (store.public_key_path, 0o600),
        (envelope_path, 0o600),
        (staged_path.parent, 0o700),
        (staged_path, 0o600),
        (staged_key, 0o600),
    ):
        assert stat.S_IMODE(path.stat().st_mode) == mode


def test_bundle_spool_is_idempotent_but_rejects_assignment_conflicts(tmp_path: Path) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    first = store.seal(assignment_id=assignment_id, branch=branch, bundle=value)

    assert store.seal(assignment_id=assignment_id, branch=branch, bundle=value) == first
    changed = value.model_copy(update={"title": "A conflicting trusted assignment title"})
    with pytest.raises(SecurityError, match="conflicts"):
        store.seal(assignment_id=assignment_id, branch=branch, bundle=changed)


@pytest.mark.parametrize("field", ["bundle", "signature"])
def test_tampered_spool_fails_closed(tmp_path: Path, field: str) -> None:
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


def test_stage_rejects_replay_insecure_files_and_spool_destinations(tmp_path: Path) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    store.seal(assignment_id=assignment_id, branch=branch, bundle=value)

    wrong_branch = f"assignment/0043-{value.slug}"
    with pytest.raises(SecurityError, match="does not match"):
        store.stage(
            assignment_id=assignment_id,
            branch=wrong_branch,
            commit_sha="a" * 40,
            destination=tmp_path / "runner" / "trusted" / "assignment-bundle.json",
            verification_key_destination=(
                tmp_path / "runner" / "trusted" / "evaluator-signing.pub"
            ),
        )
    with pytest.raises(SecurityError, match="outside the private spool"):
        store.stage(
            assignment_id=assignment_id,
            branch=branch,
            commit_sha="a" * 40,
            destination=store.spool / "export.json",
            verification_key_destination=tmp_path / "runner" / "evaluator-signing.pub",
        )

    alias = tmp_path / "envelope-hardlink.json"
    os.link(store.path_for(assignment_id), alias)
    with pytest.raises(SecurityError, match="hard links"):
        store.load(assignment_id)
    alias.unlink()

    store.path_for(assignment_id).chmod(0o644)
    with pytest.raises(SecurityError, match="owner-readable only"):
        store.load(assignment_id)


def test_runner_envelope_rejects_forgery_and_expiration(tmp_path: Path) -> None:
    value = bundle()
    assignment_id, branch = identity(value)
    store = TrustedBundleStore(tmp_path / "state")
    store.seal(assignment_id=assignment_id, branch=branch, bundle=value)
    envelope_path = tmp_path / "runner" / "trusted" / "assignment-bundle.json"
    key_path = tmp_path / "runner" / "trusted" / "evaluator-signing.pub"
    staged = store.stage(
        assignment_id=assignment_id,
        branch=branch,
        commit_sha="a" * 40,
        destination=envelope_path,
        verification_key_destination=key_path,
        ttl_seconds=60,
    )
    payload = json.loads(envelope_path.read_text(encoding="utf-8"))
    payload["bundle"]["title"] = "Attacker-controlled evaluator"
    forged_bundle = AssignmentBundle.model_validate(payload["bundle"])
    payload["bundle_digest"] = assignment_bundle_digest(forged_bundle)
    payload["binding_digest"] = assignment_binding_digest(
        assignment_id, branch, payload["bundle_digest"]
    )
    payload["signature"] = "ed25519:" + "0" * 128
    envelope_path.write_text(json.dumps(payload), encoding="utf-8")
    envelope_path.chmod(0o600)
    with pytest.raises(SecurityError, match="signature verification failed"):
        read_provisioned_envelope(envelope_path, verification_key_path=key_path)

    store.stage(
        assignment_id=assignment_id,
        branch=branch,
        commit_sha="a" * 40,
        destination=envelope_path,
        verification_key_destination=key_path,
        ttl_seconds=60,
    )
    issued = datetime.fromisoformat(staged.issued_at)
    with pytest.raises(SecurityError, match="expired"):
        read_provisioned_envelope(
            envelope_path,
            verification_key_path=key_path,
            now=issued.astimezone(UTC) + timedelta(minutes=2),
        )


def test_stage_evaluator_cli_validates_database_branch_before_export(
    initialized: tuple[Database, CurriculumPackage],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, curriculum = initialized
    request = AssignmentRequest(
        learner_id="learner",
        curriculum_id=curriculum.metadata.id,
        profile_id=curriculum.metadata.default_profile,
        target_concepts=["programming.invariants"],
        target_difficulty=4,
        context=LearnerContext(),
    )
    value = CurriculumAssignmentGenerator(curriculum).generate(request)
    validation = AssignmentValidator().validate(value, request, run_reference=False)
    created = AssignmentService(database).create(request, value, validation)
    assignment_id = str(created["id"])
    branch = str(created["branch_name"])
    data_dir = tmp_path / "runtime"
    store = TrustedBundleStore(data_dir)
    store.seal(assignment_id=assignment_id, branch=branch, bundle=value)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(data_dir),
                "database_path": str(database.path),
                "active_curriculum": curriculum.metadata.id,
                "active_profile": curriculum.metadata.default_profile,
                "learner_id": "learner",
                "codex": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "runner" / "trusted" / "assignment-bundle.json"
    verification_key = tmp_path / "runner" / "trusted" / "evaluator-signing.pub"
    runner = CliRunner()

    class VerifiedGitHub:
        def __init__(self, _: object) -> None:
            pass

        def verify_evaluator_run(self, run_id: int) -> dict[str, str]:
            assert run_id == 700
            return {
                "assignment_id": assignment_id,
                "branch": branch,
                "commit_sha": "a" * 40,
                "workflow_commit": "f" * 40,
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr("adaptive_tutor.cli.GitHubClient", VerifiedGitHub)

    wrong = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "stage-evaluator",
            assignment_id,
            "--branch",
            f"assignment/9999-{value.slug}",
            "--output",
            str(output),
            "--commit-sha",
            "a" * 40,
            "--verification-key-output",
            str(verification_key),
            "--run-id",
            "700",
        ],
    )
    assert wrong.exit_code == 1
    assert "does not match the stored assignment" in wrong.output
    assert not output.exists()

    shutil.rmtree(store.root)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "stage-evaluator",
            assignment_id,
            "--branch",
            branch,
            "--output",
            str(output),
            "--commit-sha",
            "a" * 40,
            "--verification-key-output",
            str(verification_key),
            "--run-id",
            "700",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Trusted evaluator staged" in result.output
    assert store.path_for(assignment_id).is_file()
    assert (
        read_provisioned_envelope(output, verification_key_path=verification_key).assignment_id
        == assignment_id
    )
