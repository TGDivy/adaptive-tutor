from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from adaptive_tutor.codex import FixtureCodexRunner
from adaptive_tutor.config import GitHubSettings, TutorSettings
from adaptive_tutor.db import Database
from adaptive_tutor.evaluation import EvaluationService
from adaptive_tutor.models import (
    AutomatedCheck,
    AutomatedEvaluation,
    LearnerContext,
    QualitativeEvaluation,
)
from adaptive_tutor.orchestrator import TutorOrchestrator


class ControlledGitHub:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.reviews: list[str] = []
        self.comments: list[str] = []

    def publish_assignment(self, **kwargs: Any) -> dict[str, Any]:
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
            artifact_digest="sha256:" + hashlib.sha256(b"controlled").hexdigest(),
        )
        return evidence.model_dump_json().encode()

    def post_review(self, pull_number: int, body: str, **_: Any) -> int:
        assert pull_number == 42
        self.reviews.append(body)
        return 1

    def post_comment(self, issue_number: int, body: str) -> int:
        assert issue_number == 42
        self.comments.append(body)
        return 1


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
    branch = created["branch_name"]
    orchestrator.record_submission(
        {
            "ref": f"refs/heads/{branch}",
            "after": "a" * 40,
            "head_commit": {"message": "solution\n\nConfidence: 85"},
        }
    )
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
