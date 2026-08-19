from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adaptive_tutor.cli import app
from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.errors import SecurityError
from adaptive_tutor.evaluation import EvidenceNormalizer
from adaptive_tutor.generation import CurriculumAssignmentGenerator
from adaptive_tutor.models import (
    AssignmentBundle,
    AssignmentFile,
    AssignmentRequest,
    ExerciseType,
    LearnerContext,
)
from adaptive_tutor.runner import evaluate_workspace_to_file
from adaptive_tutor.trusted_bundles import TrustedBundleStore


def executable_bundle() -> AssignmentBundle:
    package = CurriculumLoader().load(bundled_curriculum_path())
    request = AssignmentRequest(
        learner_id="runner-test",
        curriculum_id=package.metadata.id,
        profile_id=package.metadata.default_profile,
        target_concepts=["programming.invariants"],
        target_difficulty=4,
        context=LearnerContext(allowed_formats=[ExerciseType.DEBUGGING]),
    )
    bundle = CurriculumAssignmentGenerator(package).generate(request)
    environment_test = AssignmentFile(
        path="tests/test_environment.py",
        role="public_test",
        content=(
            "import os\n\n"
            "def test_credentials_are_absent():\n"
            "    for name in ('OPENAI_API_KEY', 'GITHUB_TOKEN', "
            "'ADAPTIVE_TUTOR_GITHUB_TOKEN'):\n"
            "        assert name not in os.environ\n"
        ),
    )
    return bundle.model_copy(update={"files": [*bundle.files, environment_test]})


def write_solved_workspace(bundle: AssignmentBundle, workspace: Path) -> None:
    files = {item.path: item for item in bundle.files}
    replacements = bundle.hidden_evaluator["reference_replacements"]
    for item in bundle.files:
        if item.role not in {"instructions", "starter", "public_test"}:
            continue
        target = workspace / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        source = replacements.get(item.path)
        content = files[source].content if source else item.content
        target.write_text(content, encoding="utf-8")


def provision_bundle(
    bundle: AssignmentBundle,
    root: Path,
    workspace: Path,
    *,
    assignment_id: str = "A-0042",
) -> tuple[Path, Path, str]:
    branch = f"assignment/{assignment_id.removeprefix('A-')}-{bundle.slug}"
    store = TrustedBundleStore(root / "state")
    envelope = store.seal(
        assignment_id=assignment_id,
        branch=branch,
        bundle=bundle,
    )
    bundle_path = root / "trusted" / "assignment-bundle.json"
    key_path = root / "trusted" / "evaluator-signing.pub"
    store.stage(
        assignment_id=assignment_id,
        branch=branch,
        commit_sha="a" * 40,
        destination=bundle_path,
        verification_key_destination=key_path,
    )
    metadata = workspace / ".adaptive-tutor" / "assignment.json"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "id": assignment_id,
                "branch": branch,
                "evaluator_binding": envelope.binding_digest,
                "evaluator_key_id": envelope.key_id,
            }
        ),
        encoding="utf-8",
    )
    return bundle_path, key_path, branch


def test_hidden_cli_evaluator_writes_verified_credential_free_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    evidence_path = tmp_path / "evidence" / "adaptive-tutor-evidence.json"
    write_solved_workspace(bundle, workspace)
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-tests")

    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            "--bundle",
            str(bundle_path),
            "--verification-key",
            str(key_path),
            "--workspace",
            str(workspace),
            "--output",
            str(evidence_path),
            "--assignment-id",
            "A-0042",
            "--branch",
            branch,
            "--commit-sha",
            "a" * 40,
        ],
    )

    assert result.exit_code == 0, result.output
    evidence = EvidenceNormalizer.parse(evidence_path.read_bytes())
    assert evidence.assignment_id == "A-0042"
    assert evidence.commit_sha == "a" * 40
    assert evidence.learner_passed is True
    assert evidence.evaluator_binding is not None
    assert evidence.evaluator_key_id is not None
    assert evidence.artifact_digest == evidence.computed_digest()
    assert not bundle_path.exists()
    assert not key_path.exists()
    assert "passed" in result.output


def test_evaluator_requires_trusted_input_and_output_outside_workspace(
    tmp_path: Path,
) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.chmod(0o700)
    store = TrustedBundleStore(tmp_path / "state")
    branch = f"assignment/0042-{bundle.slug}"
    store.seal(assignment_id="A-0042", branch=branch, bundle=bundle)
    bundle_path = workspace / "assignment-bundle.json"
    key_path = workspace / "evaluator-signing.pub"
    store.stage(
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
        destination=bundle_path,
        verification_key_destination=key_path,
    )

    with pytest.raises(SecurityError, match="bundle must be outside"):
        evaluate_workspace_to_file(
            bundle_path=bundle_path,
            verification_key_path=key_path,
            workspace=workspace,
            output_path=tmp_path / "evidence.json",
            assignment_id="A-0042",
            branch=branch,
            commit_sha="a" * 40,
        )

    trusted = tmp_path / "trusted" / "assignment-bundle.json"
    trusted_key = tmp_path / "trusted" / "evaluator-signing.pub"
    store.stage(
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
        destination=trusted,
        verification_key_destination=trusted_key,
    )
    with pytest.raises(SecurityError, match="output must be outside"):
        evaluate_workspace_to_file(
            bundle_path=trusted,
            verification_key_path=trusted_key,
            workspace=workspace,
            output_path=workspace / "adaptive-tutor-evidence.json",
            assignment_id="A-0042",
            branch=branch,
            commit_sha="a" * 40,
        )


def test_evaluator_rejects_wrong_branch_and_public_binding(tmp_path: Path) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)

    with pytest.raises(SecurityError, match="does not match assignment"):
        evaluate_workspace_to_file(
            bundle_path=bundle_path,
            verification_key_path=key_path,
            workspace=workspace,
            output_path=tmp_path / "evidence.json",
            assignment_id="A-0042",
            branch=f"assignment/0043-{bundle.slug}",
            commit_sha="a" * 40,
        )

    metadata_path = workspace / ".adaptive-tutor" / "assignment.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["evaluator_binding"] = "sha256:" + "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(SecurityError, match="metadata does not match"):
        evaluate_workspace_to_file(
            bundle_path=bundle_path,
            verification_key_path=key_path,
            workspace=workspace,
            output_path=tmp_path / "evidence.json",
            assignment_id="A-0042",
            branch=branch,
            commit_sha="a" * 40,
        )


@pytest.mark.parametrize("link_kind", ["file", "parent"])
def test_evaluator_rejects_submission_symlinks(tmp_path: Path, link_kind: str) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)
    candidate = next(
        item
        for item in bundle.files
        if item.role in {"starter", "public_test"}
        and (link_kind == "file" or "/" in item.path)
    )
    target = workspace / candidate.path
    if link_kind == "file":
        outside = tmp_path / "outside.py"
        outside.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        target.unlink()
        target.symlink_to(outside)
    else:
        parent = workspace / Path(candidate.path).parts[0]
        outside_parent = tmp_path / "outside-parent"
        shutil.copytree(parent, outside_parent)
        shutil.rmtree(parent)
        parent.symlink_to(outside_parent, target_is_directory=True)

    with pytest.raises(SecurityError, match="not safely readable"):
        evaluate_workspace_to_file(
            bundle_path=bundle_path,
            verification_key_path=key_path,
            workspace=workspace,
            output_path=tmp_path / "evidence.json",
            assignment_id="A-0042",
            branch=branch,
            commit_sha="a" * 40,
        )
