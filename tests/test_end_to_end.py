from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from adaptive_tutor.codex import FixtureCodexRunner
from adaptive_tutor.config import GitHubSettings, TutorSettings
from adaptive_tutor.db import Database
from adaptive_tutor.errors import (
    ConfigurationError,
    ExternalServiceError,
    InfrastructureError,
    SecurityError,
)
from adaptive_tutor.evaluation import EvaluationService
from adaptive_tutor.models import (
    AutomatedCheck,
    AutomatedEvaluation,
    LearnerContext,
    QualitativeEvaluation,
)
from adaptive_tutor.orchestrator import TutorOrchestrator
from adaptive_tutor.runner import evaluator_kit_digest
from adaptive_tutor.trusted_bundles import (
    PublicEvaluatorManifest,
    TrustedBundleStore,
    public_manifest_digest,
)

WORKFLOW_DIGEST = "sha256:" + "c" * 64
WORKFLOW_COMMIT = "f" * 40
EVALUATOR_REF = "e" * 40
REPOSITORY_ID = 12345


class ControlledGitHub:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.reviews: list[str] = []
        self.comments: list[str] = []
        self.publish_failures = 0
        self.review_failure_mode: str | None = None
        self.review_failed = False
        self.trusted_spool: Path | None = None
        self.dispatches: list[dict[str, str]] = []
        self.tamper_evaluator_binding = False
        self.evaluator_error = False
        self.preflight_failure = False
        self.control: dict[str, Any] = {}

    def preflight_assignment_publication(self) -> dict[str, Any]:
        if self.preflight_failure:
            raise ConfigurationError("GitHub owner and workspace repository are required")
        return {"private": True, "permissions": {"push": True}}

    def verify_evaluator_control(self, **kwargs: Any) -> dict[str, str | int]:
        assert kwargs == {
            "expected_repository_id": REPOSITORY_ID,
            "expected_workflow_digest": WORKFLOW_DIGEST,
            "expected_key_id": self.control["evaluator_key_id"],
        }
        return {
            "repository_id": REPOSITORY_ID,
            "repository_full_name": "owner/learning-workspace",
            "default_branch": "main",
            "workflow_commit": WORKFLOW_COMMIT,
            "workflow_digest": WORKFLOW_DIGEST,
            "evaluator_key_id": self.control["evaluator_key_id"],
        }

    def publish_assignment(self, **kwargs: Any) -> dict[str, Any]:
        metadata = json.loads(kwargs["files"][".adaptive-tutor/assignment.json"])
        if self.trusted_spool is not None:
            assert (self.trusted_spool / f"{metadata['id']}.json").is_file()
        if self.publish_failures:
            self.publish_failures -= 1
            raise ExternalServiceError("temporary publish failure", retryable=True)
        self.files = dict(kwargs["files"])
        return {
            "pull_number": 42,
            "url": "https://github.com/owner/learning-workspace/pull/42",
            "head_sha": "1" * 40,
            "branch": kwargs["branch"],
        }

    def get_file(self, path: str, ref: str) -> str:
        assert len(ref) == 40
        if path == "ANSWER.md":
            return "Invariant: occupancy distinguishes empty and full. Confidence: 85"
        return self.files[path]

    def download_evidence(self, run_id: int) -> bytes:
        commit_sha = {700: "a" * 40, 701: "b" * 40}[run_id]
        now = datetime.now(UTC)
        dispatch = self.dispatches[0 if run_id == 700 else -1]
        metadata = json.loads(self.files[".adaptive-tutor/assignment.json"])
        evidence = AutomatedEvaluation(
            assignment_id="A-0001",
            commit_sha=commit_sha,
            checks=[
                AutomatedCheck(
                    name="unit and hidden tests",
                    status="error" if self.evaluator_error else "pass",
                    category="test",
                    summary=(
                        "isolated evaluator unavailable"
                        if self.evaluator_error
                        else "all checks passed"
                    ),
                )
            ],
            started_at=now,
            completed_at=now,
            runner="github-actions",
            evaluator_key_id=metadata["evaluator_key_id"],
            dispatch_nonce=dispatch["dispatch_nonce"],
            manifest_digest=(
                "sha256:" + "0" * 64
                if self.tamper_evaluator_binding
                else dispatch["manifest_digest"]
            ),
            workflow_digest=WORKFLOW_DIGEST,
            workflow_commit=WORKFLOW_COMMIT,
            evaluator_ref=dispatch["evaluator_ref"],
            evaluator_kit_digest=dispatch["evaluator_kit_digest"],
            repository_id=REPOSITORY_ID,
            artifact_digest="sha256:"
            + hashlib.sha256(f"controlled:{commit_sha}".encode()).hexdigest(),
        ).with_computed_digest()
        return evidence.model_dump_json().encode()

    def dispatch_evaluator(self, **kwargs: str) -> None:
        self.dispatches.append(dict(kwargs))

    def verify_evaluator_run(self, run_id: int, **kwargs: Any) -> dict[str, str | int]:
        commit_sha = {700: "a" * 40, 701: "b" * 40}[run_id]
        dispatch = self.dispatches[0 if run_id == 700 else -1]
        assert kwargs == {}
        return {
            "assignment_id": "A-0001",
            "commit_sha": commit_sha,
            "dispatch_nonce": dispatch["dispatch_nonce"],
            "evaluator_ref": dispatch["evaluator_ref"],
            "workflow_commit": WORKFLOW_COMMIT,
            "workflow_digest": WORKFLOW_DIGEST,
            "repository_id": REPOSITORY_ID,
        }

    def post_review(self, pull_number: int, body: str, **_: Any) -> int:
        assert pull_number == 42
        self.reviews.append(body)
        return 1

    def ensure_review(self, pull_number: int, body: str, **kwargs: Any) -> int:
        marker = str(kwargs["marker"])
        for index, review in enumerate(self.reviews, 1):
            if marker in review:
                return index
        if self.review_failure_mode == "before" and not self.review_failed:
            self.review_failed = True
            raise ExternalServiceError("review transport failed", retryable=True)
        review_id = self.post_review(pull_number, body, **kwargs)
        if self.review_failure_mode == "after" and not self.review_failed:
            self.review_failed = True
            raise ExternalServiceError("connection dropped after delivery", retryable=True)
        return review_id

    def post_comment(self, issue_number: int, body: str) -> int:
        assert issue_number == 42
        self.comments.append(body)
        return 1

    def ensure_comment(self, issue_number: int, body: str, **kwargs: Any) -> int:
        marker = str(kwargs["marker"])
        for index, comment in enumerate(self.comments, 1):
            if marker in comment:
                return index
        return self.post_comment(issue_number, body)


def fixture() -> QualitativeEvaluation:
    return QualitativeEvaluation.model_validate_json(
        Path("curricula/systems-foundations/fixtures/demo-evaluation.json").read_text()
    )


def configure_evaluator_control(
    database: Database,
    settings: TutorSettings,
    github: ControlledGitHub,
) -> None:
    key_text = TrustedBundleStore(settings.data_dir).public_verification_key().strip()
    key_id = hashlib.sha256(bytes.fromhex(key_text.removeprefix("ed25519:"))).hexdigest()[:16]
    configured_at = datetime.now(UTC).isoformat()
    database.execute(
        """
        INSERT INTO evaluator_control_planes(
            repository_id, repository_full_name, default_branch, workflow_path,
            workflow_commit, workflow_digest, evaluator_ref, evaluator_kit_digest,
            evaluator_key_id, configured_at, verified_at
        ) VALUES (?, ?, 'main', '.github/workflows/adaptive-tutor-evaluate.yml',
                  ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            REPOSITORY_ID,
            "owner/learning-workspace",
            WORKFLOW_COMMIT,
            WORKFLOW_DIGEST,
            EVALUATOR_REF,
            evaluator_kit_digest(),
            key_id,
            configured_at,
            configured_at,
        ),
    )
    github.control = {"evaluator_key_id": key_id}


def test_controlled_end_to_end_assignment_evaluation_and_next_selection(
    initialized: tuple[Database, object], tmp_path: Path
) -> None:
    database, _ = initialized
    github = ControlledGitHub()
    settings = TutorSettings(
        data_dir=tmp_path,
        learner_id="learner",
        github=GitHubSettings(owner="owner", workspace_repo="learning-workspace"),
    )
    github.trusted_spool = settings.data_dir / "trusted-evaluators" / "spool"
    configure_evaluator_control(database, settings, github)
    orchestrator = TutorOrchestrator(
        settings,
        database,
        github,  # type: ignore[arg-type]
        EvaluationService(database, FixtureCodexRunner(fixture())),
    )
    created = orchestrator.create_next_assignment(LearnerContext(available_minutes=45))
    assert created["id"] == "A-0001"
    assert created["pull_number"] == 42
    assert "reference/bounded_queue.py" not in github.files
    metadata = json.loads(github.files[".adaptive-tutor/assignment.json"])
    envelope = TrustedBundleStore(settings.data_dir).load("A-0001")
    manifest = PublicEvaluatorManifest.model_validate_json(
        github.files[".adaptive-tutor/evaluator-manifest.json"]
    )
    assert metadata["id"] == envelope.assignment_id
    assert metadata["branch"] == envelope.branch
    assert metadata["evaluator_manifest_digest"] == public_manifest_digest(manifest)
    assert metadata["evaluator_key_id"] == envelope.key_id
    assert metadata["evaluator_kit_digest"] == evaluator_kit_digest()
    assert "hidden_evaluator" not in github.files[".adaptive-tutor/assignment.json"]
    assert "hidden_evaluator" not in github.files[".adaptive-tutor/evaluator-manifest.json"]
    branch = created["branch_name"]
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "1" * 40,
            "head_commit": {"message": "tutor publication"},
        }
    )
    assert database.fetch_one("SELECT COUNT(*) count FROM attempts") == {"count": 0}
    assert github.dispatches == []
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "a" * 40,
            "head_commit": {"message": "solution\n\nConfidence: 85"},
        }
    )
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "a" * 40,
            "head_commit": {"message": "solution\n\nConfidence: 85"},
        }
    )
    assert len(github.dispatches) == 1
    first_dispatch = github.dispatches[0]
    assert first_dispatch == {
        "assignment_id": "A-0001",
        "branch": branch,
        "commit_sha": "a" * 40,
        "dispatch_nonce": first_dispatch["dispatch_nonce"],
        "manifest_digest": metadata["evaluator_manifest_digest"],
        "evaluator_ref": EVALUATOR_REF,
        "evaluator_kit_digest": evaluator_kit_digest(),
    }
    assert len(first_dispatch["dispatch_nonce"]) == 32
    orchestrator.process_ci_result(
        {
            "action": "completed",
            "workflow_run": {
                "id": 700,
                "head_branch": branch,
                "head_sha": "a" * 40,
                "conclusion": "success",
            },
        }
    )
    assert len(github.reviews) == 1
    assert "Adaptive Tutor review" in github.reviews[0]
    assignment = database.fetch_one(
        "SELECT status, current_stage FROM assignments WHERE id='A-0001'"
    )
    assert assignment == {"status": "follow_up", "current_stage": 2}
    assert len(github.comments) == 1
    assert "Stage 2: Representation follow-up" in github.comments[0]
    orchestrator.evaluations.grader = FixtureCodexRunner(
        fixture().model_copy(
            update={
                "follow_up": "new_assignment",
                "follow_up_reason": "Stage two is complete; schedule transfer work.",
            }
        )
    )
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "b" * 40,
            "head_commit": {"message": "stage two\n\nConfidence: 88"},
        }
    )
    second_dispatch = github.dispatches[-1]
    assert second_dispatch == {
        "assignment_id": "A-0001",
        "branch": branch,
        "commit_sha": "b" * 40,
        "dispatch_nonce": second_dispatch["dispatch_nonce"],
        "manifest_digest": metadata["evaluator_manifest_digest"],
        "evaluator_ref": EVALUATOR_REF,
        "evaluator_kit_digest": evaluator_kit_digest(),
    }
    assert second_dispatch["dispatch_nonce"] != first_dispatch["dispatch_nonce"]
    orchestrator.process_ci_result(
        {
            "action": "completed",
            "workflow_run": {
                "id": 701,
                "head_branch": branch,
                "head_sha": "b" * 40,
                "conclusion": "success",
            },
        }
    )
    assignment = database.fetch_one(
        "SELECT status, current_stage FROM assignments WHERE id='A-0001'"
    )
    assert assignment == {"status": "completed", "current_stage": 2}
    assert len(github.reviews) == 2
    orchestrator.process_ci_result(
        {
            "action": "completed",
            "workflow_run": {"id": 700, "conclusion": "success"},
        }
    )
    assert database.fetch_one(
        "SELECT status, current_stage FROM assignments WHERE id='A-0001'"
    ) == {"status": "completed", "current_stage": 2}
    assert len(github.reviews) == 2
    assert len(github.comments) == 1
    mastery = database.fetch_one(
        """
        SELECT evidence_count, mastery_estimate FROM mastery
        WHERE concept_id='programming.invariants'
        """
    )
    assert mastery is not None
    assert mastery["evidence_count"] == 2
    assert mastery["mastery_estimate"] > 0.2
    next_created = orchestrator.create_next_assignment(LearnerContext(available_minutes=45))
    assert next_created["id"] == "A-0002"


def test_assignment_publication_resumes_without_generating_a_duplicate(
    initialized: tuple[Database, object], tmp_path: Path
) -> None:
    database, _ = initialized
    github = ControlledGitHub()
    github.publish_failures = 1
    settings = TutorSettings(
        data_dir=tmp_path,
        learner_id="learner",
        github=GitHubSettings(owner="owner", workspace_repo="learning-workspace"),
    )
    github.trusted_spool = settings.data_dir / "trusted-evaluators" / "spool"
    configure_evaluator_control(database, settings, github)
    orchestrator = TutorOrchestrator(
        settings,
        database,
        github,  # type: ignore[arg-type]
        EvaluationService(database, FixtureCodexRunner(fixture())),
    )

    with pytest.raises(ExternalServiceError, match="publish failure"):
        orchestrator.create_next_assignment(LearnerContext())
    assert database.fetch_one("SELECT id, status FROM assignments") == {
        "id": "A-0001",
        "status": "validated",
    }
    failed = database.fetch_one(
        "SELECT publication_attempted_at, publication_error FROM assignments"
    )
    assert failed is not None and failed["publication_attempted_at"]
    assert failed["publication_error"] == "temporary publish failure"

    resumed = orchestrator.create_next_assignment(LearnerContext())
    assert resumed["id"] == "A-0001"
    assert resumed["pull_number"] == 42
    assert database.fetch_one("SELECT COUNT(*) count FROM assignments") == {"count": 1}
    assert database.fetch_one("SELECT publication_error FROM assignments") == {
        "publication_error": None
    }


def test_assignment_preflight_failure_does_not_create_stranded_state(
    initialized: tuple[Database, object], tmp_path: Path
) -> None:
    database, _ = initialized
    github = ControlledGitHub()
    github.preflight_failure = True
    orchestrator = TutorOrchestrator(
        TutorSettings(data_dir=tmp_path, learner_id="learner"),
        database,
        github,  # type: ignore[arg-type]
        EvaluationService(database, FixtureCodexRunner(fixture())),
    )

    with pytest.raises(ConfigurationError, match="owner and workspace"):
        orchestrator.create_next_assignment(LearnerContext())

    assert database.fetch_one("SELECT COUNT(*) count FROM assignments") == {"count": 0}
    assert (
        database.fetch_one("SELECT value_json FROM configuration WHERE key='assignment_counter'")
        is None
    )


def test_ci_evidence_must_match_the_trusted_spool_identity(
    initialized: tuple[Database, object], tmp_path: Path
) -> None:
    database, _ = initialized
    github = ControlledGitHub()
    settings = TutorSettings(
        data_dir=tmp_path,
        learner_id="learner",
        github=GitHubSettings(owner="owner", workspace_repo="learning-workspace"),
    )
    github.trusted_spool = settings.data_dir / "trusted-evaluators" / "spool"
    configure_evaluator_control(database, settings, github)
    orchestrator = TutorOrchestrator(
        settings,
        database,
        github,  # type: ignore[arg-type]
        EvaluationService(database, FixtureCodexRunner(fixture())),
    )
    created = orchestrator.create_next_assignment(LearnerContext())
    branch = str(created["branch_name"])
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "a" * 40,
            "head_commit": {"message": "solution"},
        }
    )
    github.tamper_evaluator_binding = True

    with pytest.raises(SecurityError, match="trusted evaluator"):
        orchestrator.process_ci_result(
            {
                "action": "completed",
                "workflow_run": {"id": 700, "conclusion": "success"},
            }
        )
    assert database.fetch_one("SELECT COUNT(*) count FROM automated_evaluations") == {"count": 0}
    assert database.fetch_one("SELECT COUNT(*) count FROM mastery_evidence") == {"count": 0}


def test_evaluator_operational_error_never_becomes_learner_evidence(
    initialized: tuple[Database, object], tmp_path: Path
) -> None:
    database, _ = initialized
    github = ControlledGitHub()
    settings = TutorSettings(
        data_dir=tmp_path,
        learner_id="learner",
        github=GitHubSettings(owner="owner", workspace_repo="learning-workspace"),
    )
    github.trusted_spool = settings.data_dir / "trusted-evaluators" / "spool"
    configure_evaluator_control(database, settings, github)
    orchestrator = TutorOrchestrator(
        settings,
        database,
        github,  # type: ignore[arg-type]
        EvaluationService(database, FixtureCodexRunner(fixture())),
    )
    created = orchestrator.create_next_assignment(LearnerContext())
    branch = str(created["branch_name"])
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "a" * 40,
            "head_commit": {"message": "solution"},
        }
    )
    github.evaluator_error = True

    with pytest.raises(InfrastructureError, match="operational failure") as raised:
        orchestrator.process_ci_result(
            {
                "action": "completed",
                "workflow_run": {"id": 700, "conclusion": "failure"},
            }
        )

    assert raised.value.retryable is True
    assert database.fetch_one("SELECT COUNT(*) count FROM automated_evaluations") == {"count": 0}
    assert database.fetch_one("SELECT COUNT(*) count FROM qualitative_evaluations") == {"count": 0}
    assert database.fetch_one("SELECT COUNT(*) count FROM mastery_evidence") == {"count": 0}


@pytest.mark.parametrize("failure_mode", ["before", "after"])
def test_review_delivery_resumes_after_grading_or_delivery_crash(
    initialized: tuple[Database, object], tmp_path: Path, failure_mode: str
) -> None:
    database, _ = initialized
    github = ControlledGitHub()
    github.review_failure_mode = failure_mode
    settings = TutorSettings(
        data_dir=tmp_path,
        learner_id="learner",
        github=GitHubSettings(owner="owner", workspace_repo="learning-workspace"),
    )
    github.trusted_spool = settings.data_dir / "trusted-evaluators" / "spool"
    configure_evaluator_control(database, settings, github)
    orchestrator = TutorOrchestrator(
        settings,
        database,
        github,  # type: ignore[arg-type]
        EvaluationService(database, FixtureCodexRunner(fixture())),
    )
    created = orchestrator.create_next_assignment(LearnerContext())
    branch = str(created["branch_name"])
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "a" * 40,
            "head_commit": {"message": "solution\n\nConfidence: 85"},
        }
    )
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": 700,
            "head_branch": branch,
            "head_sha": "a" * 40,
            "conclusion": "success",
        },
    }

    with pytest.raises(ExternalServiceError):
        orchestrator.process_ci_result(payload)
    assert database.fetch_one("SELECT COUNT(*) count FROM qualitative_evaluations") == {"count": 1}
    assert database.fetch_one("SELECT COUNT(*) count FROM mastery_evidence") == {"count": 1}
    assert len(github.reviews) == int(failure_mode == "after")

    orchestrator.process_ci_result(payload)
    assert len(github.reviews) == 1
    assert database.fetch_one(
        "SELECT review_posted_at IS NOT NULL posted FROM qualitative_evaluations"
    ) == {"posted": 1}
    assert database.fetch_one("SELECT COUNT(*) count FROM qualitative_evaluations") == {"count": 1}
    assert database.fetch_one("SELECT COUNT(*) count FROM mastery_evidence") == {"count": 1}
    assert database.fetch_one("SELECT status, current_stage FROM assignments") == {
        "status": "follow_up",
        "current_stage": 2,
    }
