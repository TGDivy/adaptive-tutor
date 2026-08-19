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
from adaptive_tutor.errors import ExternalServiceError, SecurityError
from adaptive_tutor.evaluation import EvaluationService
from adaptive_tutor.models import (
    AutomatedCheck,
    AutomatedEvaluation,
    LearnerContext,
    QualitativeEvaluation,
)
from adaptive_tutor.orchestrator import TutorOrchestrator
from adaptive_tutor.trusted_bundles import TrustedBundleStore


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
        assert run_id == 700
        now = datetime.now(UTC)
        assert self.trusted_spool is not None
        data_dir = self.trusted_spool.parent.parent
        trusted = TrustedBundleStore(data_dir).load("A-0001")
        evidence = AutomatedEvaluation(
            assignment_id="A-0001",
            commit_sha="a" * 40,
            checks=[
                AutomatedCheck(
                    name="unit and hidden tests",
                    status="pass",
                    category="test",
                    summary="all checks passed",
                )
            ],
            started_at=now,
            completed_at=now,
            runner="github-actions",
            evaluator_binding=(
                "sha256:" + "0" * 64
                if self.tamper_evaluator_binding
                else trusted.binding_digest
            ),
            evaluator_key_id=trusted.key_id,
            artifact_digest="sha256:" + hashlib.sha256(b"controlled").hexdigest(),
        ).with_computed_digest()
        return evidence.model_dump_json().encode()

    def dispatch_evaluator(self, **kwargs: str) -> None:
        self.dispatches.append(dict(kwargs))

    def verify_evaluator_run(self, run_id: int, **kwargs: Any) -> dict[str, str]:
        assert run_id == 700
        assert kwargs == {}
        return {
            "assignment_id": "A-0001",
            "branch": "assignment/0001-bounded-work-queue",
            "commit_sha": "a" * 40,
            "workflow_commit": "f" * 40,
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
    assert metadata["id"] == envelope.assignment_id
    assert metadata["branch"] == envelope.branch
    assert metadata["evaluator_binding"] == envelope.binding_digest
    assert metadata["evaluator_key_id"] == envelope.key_id
    assert "hidden_evaluator" not in github.files[".adaptive-tutor/assignment.json"]
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
    assert github.dispatches == [
        {"assignment_id": "A-0001", "branch": branch, "commit_sha": "a" * 40}
    ]
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
    assignment = database.fetch_one("SELECT status FROM assignments WHERE id='A-0001'")
    assert assignment == {"status": "completed"}
    mastery = database.fetch_one(
        """
        SELECT evidence_count, mastery_estimate FROM mastery
        WHERE concept_id='programming.invariants'
        """
    )
    assert mastery is not None
    assert mastery["evidence_count"] == 1
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

    resumed = orchestrator.create_next_assignment(LearnerContext())
    assert resumed["id"] == "A-0001"
    assert resumed["pull_number"] == 42
    assert database.fetch_one("SELECT COUNT(*) count FROM assignments") == {"count": 1}


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
    assert database.fetch_one("SELECT COUNT(*) count FROM automated_evaluations") == {
        "count": 0
    }
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
    assert database.fetch_one("SELECT COUNT(*) count FROM qualitative_evaluations") == {
        "count": 1
    }
    assert database.fetch_one("SELECT COUNT(*) count FROM mastery_evidence") == {"count": 1}
    assert len(github.reviews) == int(failure_mode == "after")

    orchestrator.process_ci_result(payload)
    assert len(github.reviews) == 1
    assert database.fetch_one(
        "SELECT review_posted_at IS NOT NULL posted FROM qualitative_evaluations"
    ) == {"posted": 1}
    assert database.fetch_one("SELECT COUNT(*) count FROM qualitative_evaluations") == {
        "count": 1
    }
    assert database.fetch_one("SELECT COUNT(*) count FROM mastery_evidence") == {"count": 1}
    assert database.fetch_one("SELECT status FROM assignments") == {"status": "completed"}
