from __future__ import annotations

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


def test_hidden_cli_evaluator_writes_verified_credential_free_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = executable_bundle()
    trusted = tmp_path / "trusted"
    workspace = tmp_path / "workspace"
    evidence_path = tmp_path / "evidence" / "adaptive-tutor-evidence.json"
    trusted.mkdir()
    bundle_path = trusted / "assignment-bundle.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    write_solved_workspace(bundle, workspace)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-tests")

    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            "--bundle",
            str(bundle_path),
            "--workspace",
            str(workspace),
            "--output",
            str(evidence_path),
            "--assignment-id",
            "A-0042",
            "--commit-sha",
            "a" * 40,
        ],
    )

    assert result.exit_code == 0, result.output
    evidence = EvidenceNormalizer.parse(evidence_path.read_bytes())
    assert evidence.assignment_id == "A-0042"
    assert evidence.commit_sha == "a" * 40
    assert evidence.learner_passed is True
    assert evidence.artifact_digest == evidence.computed_digest()
    assert "passed" in result.output


def test_evaluator_requires_trusted_input_and_output_outside_workspace(
    tmp_path: Path,
) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle_path = workspace / "assignment-bundle.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")

    with pytest.raises(SecurityError, match="bundle must be outside"):
        evaluate_workspace_to_file(
            bundle_path=bundle_path,
            workspace=workspace,
            output_path=tmp_path / "evidence.json",
            assignment_id="A-0042",
            commit_sha="a" * 40,
        )

    trusted = tmp_path / "trusted.json"
    trusted.write_text(bundle.model_dump_json(), encoding="utf-8")
    with pytest.raises(SecurityError, match="output must be outside"):
        evaluate_workspace_to_file(
            bundle_path=trusted,
            workspace=workspace,
            output_path=workspace / "adaptive-tutor-evidence.json",
            assignment_id="A-0042",
            commit_sha="a" * 40,
        )
