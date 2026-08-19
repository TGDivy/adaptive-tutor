"""Credential-free deterministic end-to-end product demonstration."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .assignments import AssignmentService, AssignmentValidator, TemplateAssignmentGenerator
from .codex import FixtureCodexRunner
from .curriculum import CurriculumLoader, bundled_curriculum_path
from .db import Database
from .evaluation import EvaluationService
from .models import (
    AssignmentRequest,
    AutomatedCheck,
    AutomatedEvaluation,
    ConceptEvidence,
    LearnerContext,
    QualitativeEvaluation,
)
from .reporting import ReportDocument, ReportService
from .scheduler import AdaptiveScheduler
from .state import StatusService
from .time import iso_now, utc_now


@dataclass(frozen=True)
class DemoResult:
    database_path: str
    curriculum: str
    recommendation: dict[str, Any]
    assignment: dict[str, Any]
    validation_checks: dict[str, str]
    automated_evidence: dict[str, Any]
    qualitative_evaluation: dict[str, Any]
    status: dict[str, Any]
    report: ReportDocument


def run_demo(data_dir: Path | None = None) -> DemoResult:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if data_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="adaptive-tutor-demo-")
        root = Path(temporary.name)
    else:
        root = data_dir.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    try:
        database = Database(root / "demo.sqlite3")
        database.migrate()
        package = CurriculumLoader().load(bundled_curriculum_path())
        CurriculumLoader().persist(package, database, "demo-learner")
        context = LearnerContext(available_minutes=45, energy="medium")
        recommendation = AdaptiveScheduler(database).recommend(
            "demo-learner", package.metadata.id, package.metadata.default_profile, context, limit=1
        )[0]
        request = AssignmentRequest(
            learner_id="demo-learner",
            curriculum_id=package.metadata.id,
            profile_id=package.metadata.default_profile,
            target_concepts=[recommendation.concept_id],
            target_difficulty=recommendation.target_difficulty,
            context=context.model_copy(update={"allowed_formats": [recommendation.exercise_type]}),
        )
        bundle = TemplateAssignmentGenerator().generate(request)
        validation = AssignmentValidator().validate(bundle, request)
        created = AssignmentService(database).create(request, bundle, validation)
        assignment_id = str(created["id"])
        commit_sha = "d" * 40
        attempt_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        database.execute(
            """
            INSERT INTO attempts(
                id, assignment_id, commit_sha, learner_confidence,
                submission_source, submitted_at
            ) VALUES (?, ?, ?, 82, 'local_demo', ?)
            """,
            (attempt_id, assignment_id, commit_sha, iso_now()),
        )
        automated = AutomatedEvaluation(
            assignment_id=assignment_id,
            commit_sha=commit_sha,
            checks=[
                AutomatedCheck(
                    name="public tests",
                    status="pass",
                    category="test",
                    summary="Public regression checks passed",
                    duration_ms=84,
                ),
                AutomatedCheck(
                    name="hidden boundary tests",
                    status="pass",
                    category="integration",
                    summary="Wraparound and repeated-cycle checks passed",
                    duration_ms=131,
                ),
                AutomatedCheck(
                    name="static policy",
                    status="pass",
                    category="policy",
                    summary="No forbidden API or credential access",
                    duration_ms=12,
                ),
            ],
            started_at=now,
            completed_at=now,
            runner="local-demo-fixture",
            artifact_digest="sha256:" + hashlib.sha256(b"adaptive-tutor-demo").hexdigest(),
        )
        fixture_payload = json.loads(
            (bundled_curriculum_path() / "fixtures" / "demo-evaluation.json").read_text(
                encoding="utf-8"
            )
        )
        fixture = QualitativeEvaluation.model_validate(fixture_payload)
        fixture = fixture.model_copy(
            update={
                "concept_evidence": [
                    ConceptEvidence(
                        concept_id=recommendation.concept_id,
                        outcome="success",
                        strength=0.9,
                        difficulty=recommendation.target_difficulty,
                        exercise_type=recommendation.exercise_type,
                        rationale="The repair preserves the stated invariant across hidden cases.",
                        transfer_context="a bounded queue maintenance task",
                    )
                ]
            }
        )
        evaluator = EvaluationService(database, FixtureCodexRunner(fixture))
        automated_id = evaluator.persist_automated(attempt_id, automated)
        _, qualitative, _ = evaluator.grade_attempt(
            learner_id="demo-learner",
            assignment_id=assignment_id,
            attempt_id=attempt_id,
            automated_evaluation_id=automated_id,
            rubric=bundle.rubric,
            references={
                item.path: item.content for item in bundle.files if item.role == "reference"
            },
            submission={
                "ANSWER.md": (
                    "An explicit occupancy value distinguishes equal-index empty and full states. "
                    "The regression test covers fill, drain, and wraparound. Confidence: 82"
                )
            },
            trusted_instructions=package.prompts["grading"],
            prompt_version=package.metadata.version,
            learner_confidence=82,
        )
        database.execute(
            """
            UPDATE assignments SET status='completed', completed_at=?, updated_at=? WHERE id=?
            """,
            (iso_now(), iso_now(), assignment_id),
        )
        report = ReportService(database).generate(
            "demo-learner", package.metadata.id, "weekly", end=utc_now()
        )
        snapshot = StatusService(database).get_status(
            "demo-learner", package.metadata.id
        ).model_dump(mode="json")
        return DemoResult(
            database_path=str(database.path),
            curriculum=package.metadata.name,
            recommendation=recommendation.model_dump(mode="json"),
            assignment={
                "id": assignment_id,
                "title": bundle.title,
                "exercise_type": bundle.exercise_type.value,
                "difficulty": bundle.difficulty,
                "expected_minutes": bundle.expected_minutes,
                "branch": created["branch_name"],
            },
            validation_checks=validation.checks,
            automated_evidence=automated.model_dump(mode="json"),
            qualitative_evaluation=qualitative.model_dump(mode="json"),
            status=snapshot,
            report=report,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
