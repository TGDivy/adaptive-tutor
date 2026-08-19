from __future__ import annotations

import json
import secrets
import shutil
import time
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
            "import os\n"
            "import socket\n"
            "from pathlib import Path\n\n"
            "import pytest\n\n"
            "def test_credentials_are_absent():\n"
            "    for name in ('OPENAI_API_KEY', 'GITHUB_TOKEN', "
            "'ADAPTIVE_TUTOR_GITHUB_TOKEN'):\n"
            "        assert name not in os.environ\n"
            "\n"
            "def test_os_sandbox_boundaries():\n"
            "    assert not Path('/etc/passwd').exists()\n"
            "    assert not Path('/root/.ssh').exists()\n"
            "    assert not Path('/proc').exists()\n"
            "    with pytest.raises(OSError):\n"
            "        Path('/evaluation/submission/.write-probe').write_text('unsafe')\n"
            "    connection = socket.socket()\n"
            "    connection.settimeout(0.2)\n"
            "    with pytest.raises(OSError):\n"
            "        connection.connect(('198.51.100.1', 9))\n"
            "    connection.close()\n"
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


def solved_source(bundle: AssignmentBundle) -> tuple[str, str]:
    files = {item.path: item for item in bundle.files}
    replacements = bundle.hidden_evaluator["reference_replacements"]
    source_path, reference_path = next(iter(replacements.items()))
    return source_path, files[reference_path].content


def process_with_marker_exists(marker: str) -> bool:
    for command_line in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if marker.encode() in command_line.read_bytes():
                return True
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return False


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
        if item.role in {"starter", "public_test"} and (link_kind == "file" or "/" in item.path)
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


def test_learner_zero_exit_cannot_forge_passing_completion(tmp_path: Path) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    source_path, _ = solved_source(bundle)
    (workspace / source_path).write_text(
        "import os\nos._exit(0)\n",
        encoding="utf-8",
    )
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)

    evidence = evaluate_workspace_to_file(
        bundle_path=bundle_path,
        verification_key_path=key_path,
        workspace=workspace,
        output_path=tmp_path / "evidence.json",
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
    )

    assert evidence.learner_passed is False
    test_check = next(check for check in evidence.checks if check.category == "test")
    assert test_check.status == "fail"
    sandbox_check = next(check for check in evidence.checks if check.name == "ephemeral sandbox")
    assert sandbox_check.status == "pass"


def test_worker_cannot_forge_completion_through_inherited_descriptors(tmp_path: Path) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    source_path, _ = solved_source(bundle)
    (workspace / source_path).write_text(
        "import os\n"
        "for descriptor in range(3, 64):\n"
        "    try:\n"
        "        os.write(descriptor, b'{\\\"forged\\\":true}\\n')\n"
        "    except OSError:\n"
        "        pass\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)
    output = tmp_path / "evidence.json"

    evidence = evaluate_workspace_to_file(
        bundle_path=bundle_path,
        verification_key_path=key_path,
        workspace=workspace,
        output_path=output,
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
    )

    assert evidence.learner_passed is False
    assert "forged" not in output.read_text(encoding="utf-8")


def test_learner_changes_to_public_tests_are_not_trusted(tmp_path: Path) -> None:
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    public_test = next(item for item in bundle.files if item.role == "public_test")
    (workspace / public_test.path).write_text("import os\nos._exit(0)\n", encoding="utf-8")
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)

    evidence = evaluate_workspace_to_file(
        bundle_path=bundle_path,
        verification_key_path=key_path,
        workspace=workspace,
        output_path=tmp_path / "evidence.json",
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
    )

    assert evidence.learner_passed is True


def test_hidden_output_and_evidence_destination_are_quarantined(tmp_path: Path) -> None:
    marker = "PRIVATE-EVALUATOR-MARKER-7A9C"
    bundle = executable_bundle()
    bundle = bundle.model_copy(
        update={
            "files": [
                item.model_copy(update={"content": f"# {marker}\n{item.content}"})
                if item.role == "evaluator"
                else item
                for item in bundle.files
            ]
        }
    )
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    source_path, reference = solved_source(bundle)
    attack = (
        "from pathlib import Path as _AttackPath\n"
        "for _hidden in _AttackPath('/evaluation/trusted-tests/hidden').rglob('*'):\n"
        "    if _hidden.is_file():\n"
        "        print(_hidden.read_text(errors='replace'))\n"
        "for _destination in ('/adaptive-tutor-evidence.json', "
        "'/evaluation/adaptive-tutor-evidence.json'):\n"
        "    try:\n"
        "        _AttackPath(_destination).write_text('{\\\"forged\\\": true}')\n"
        "    except OSError:\n"
        "        pass\n"
    )
    (workspace / source_path).write_text(f"{attack}\n{reference}", encoding="utf-8")
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)
    output = tmp_path / "evidence" / "adaptive-tutor-evidence.json"

    evidence = evaluate_workspace_to_file(
        bundle_path=bundle_path,
        verification_key_path=key_path,
        workspace=workspace,
        output_path=output,
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
    )

    serialized = output.read_text(encoding="utf-8")
    assert evidence.learner_passed is True
    assert marker not in serialized
    assert "forged" not in serialized
    assert evidence.artifact_digest == evidence.computed_digest()


def test_sandbox_reaps_learner_child_processes(tmp_path: Path) -> None:
    marker = f"adaptive-tutor-child-{secrets.token_hex(8)}"
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    source_path, reference = solved_source(bundle)
    attack = (
        "import subprocess as _attack_subprocess\n"
        "import sys as _attack_sys\n"
        "_attack_subprocess.Popen(\n"
        f"    [_attack_sys.executable, '-c', 'import time; time.sleep(60)', '{marker}'],\n"
        "    stdin=_attack_subprocess.DEVNULL,\n"
        "    stdout=_attack_subprocess.DEVNULL,\n"
        "    stderr=_attack_subprocess.DEVNULL,\n"
        ")\n"
    )
    (workspace / source_path).write_text(f"{attack}\n{reference}", encoding="utf-8")
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)

    evidence = evaluate_workspace_to_file(
        bundle_path=bundle_path,
        verification_key_path=key_path,
        workspace=workspace,
        output_path=tmp_path / "evidence.json",
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
    )

    deadline = time.monotonic() + 2
    while process_with_marker_exists(marker) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert evidence.learner_passed is True
    assert not process_with_marker_exists(marker)


def test_learner_output_is_bounded_and_never_becomes_evidence(tmp_path: Path) -> None:
    marker = "UNTRUSTED-OUTPUT-MARKER-2D81"
    bundle = executable_bundle()
    workspace = tmp_path / "workspace"
    write_solved_workspace(bundle, workspace)
    source_path, reference = solved_source(bundle)
    attack = (
        f"import os as _attack_os\n_attack_os.write(1, b'{marker}' + b'x' * (3 * 1024 * 1024))\n"
    )
    (workspace / source_path).write_text(f"{attack}\n{reference}", encoding="utf-8")
    bundle_path, key_path, branch = provision_bundle(bundle, tmp_path, workspace)
    output = tmp_path / "evidence.json"

    evidence = evaluate_workspace_to_file(
        bundle_path=bundle_path,
        verification_key_path=key_path,
        workspace=workspace,
        output_path=output,
        assignment_id="A-0042",
        branch=branch,
        commit_sha="a" * 40,
    )

    serialized = output.read_text(encoding="utf-8")
    assert evidence.learner_passed is True
    assert len(serialized) < 16 * 1024
    assert marker not in serialized
