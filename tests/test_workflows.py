from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
PINNED_ACTION = re.compile(r"uses:\s+[^\s]+@([0-9a-f]{40})(?:\s|$)")


def test_workflows_pin_actions_and_avoid_unsafe_pr_target() -> None:
    workflows = sorted(WORKFLOW_ROOT.glob("*.yml"))
    assert {path.name for path in workflows} >= {
        "ci.yml",
        "docs.yml",
        "security.yml",
        "dependency-review.yml",
    }
    for path in workflows:
        content = path.read_text(encoding="utf-8")
        assert "pull_request_target" not in content
        uses_lines = [line for line in content.splitlines() if "uses:" in line]
        assert uses_lines
        assert all(PINNED_ACTION.search(line) for line in uses_lines), path


def test_ci_exercises_package_docs_container_and_privacy() -> None:
    content = (WORKFLOW_ROOT / "ci.yml").read_text(encoding="utf-8")
    for required in (
        "ruff check",
        "mypy",
        "pytest",
        "check-public-boundary",
        "uv run --locked python scripts/check-deployment",
        "uv run --locked python scripts/check-docs",
        "uv build",
        "adaptive-tutor demo",
        "docker build",
        "--network none",
        "bubblewrap",
        "apparmor_restrict_unprivileged_userns",
        "bwrap --die-with-parent --unshare-all",
    ):
        assert required in content


def test_workflow_yaml_and_dependabot_are_parseable() -> None:
    for path in WORKFLOW_ROOT.glob("*.yml"):
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)
    dependabot = yaml.safe_load(
        (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    ecosystems = {item["package-ecosystem"] for item in dependabot["updates"]}
    assert ecosystems == {"uv", "docker", "github-actions"}


def test_python_validation_scripts_run_inside_the_locked_environment() -> None:
    for workflow_name in ("ci.yml", "docs.yml"):
        content = (WORKFLOW_ROOT / workflow_name).read_text(encoding="utf-8")
        assert "./scripts/check-docs" not in content
        assert "uv run --locked python scripts/check-docs" in content


def test_workspace_evaluator_is_ephemeral_credential_free_and_uploads_contract() -> None:
    path = ROOT / "deploy" / "workspace" / "adaptive-tutor-evaluate.yml"
    content = path.read_text(encoding="utf-8")
    assert isinstance(yaml.safe_load(content), dict)
    for required in (
        "adaptive-tutor-ephemeral",
        "persist-credentials: false",
        "env -i",
        "adaptive-tutor evaluate",
        "assignment-bundle.json",
        "evaluator-signing.pub",
        "stat -c '%a'",
        "command -v bwrap",
        "bwrap --version",
        "workflow_dispatch:",
        "ref: ${{ inputs.commit_sha }}",
        '--branch "${ASSIGNMENT_BRANCH}"',
        '--commit-sha "${ASSIGNMENT_COMMIT}"',
        "adaptive-tutor-evidence.json",
        "name: adaptive-tutor-evidence",
        "retention-days: 14",
    ):
        assert required in content
    assert "pull_request_target" not in content
    assert '\n  push:' not in content
    uses_lines = [line for line in content.splitlines() if "uses:" in line]
    assert uses_lines and all(PINNED_ACTION.search(line) for line in uses_lines)
