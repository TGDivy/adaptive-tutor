from __future__ import annotations

import uuid
from typing import Literal

from adaptive_tutor.assignments import (
    AssignmentService,
    AssignmentValidator,
)
from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.db import Database
from adaptive_tutor.generation import CurriculumAssignmentGenerator
from adaptive_tutor.learner import LearnerModel
from adaptive_tutor.models import (
    AssignmentRequest,
    ConceptEvidence,
    DimensionScore,
    ExerciseType,
    LearnerContext,
    MisconceptionFinding,
    QualitativeEvaluation,
)
from adaptive_tutor.time import iso_now


def setup_assignment(
    database: Database,
    exercise_type: ExerciseType = ExerciseType.DEBUGGING,
) -> str:
    request = AssignmentRequest(
        learner_id="learner",
        curriculum_id="systems-foundations",
        profile_id="generalist",
        target_concepts=["programming.invariants"],
        target_difficulty=4,
        context=LearnerContext(allowed_formats=[exercise_type]),
    )
    bundle = CurriculumAssignmentGenerator(
        CurriculumLoader().load(bundled_curriculum_path())
    ).generate(request)
    validation = AssignmentValidator().validate(bundle, request, run_reference=False)
    return str(AssignmentService(database).create(request, bundle, validation)["id"])


def finish_assignment(database: Database, assignment_id: str) -> None:
    database.execute(
        "UPDATE assignments SET status='completed', completed_at=?, updated_at=? WHERE id=?",
        (iso_now(), iso_now(), assignment_id),
    )


def evaluation(
    *,
    outcome: Literal["success", "partial", "failure", "not_observed"],
    exercise_type: ExerciseType,
    action: Literal["suspect", "confirm", "challenge", "resolve", "recur"] | None = None,
    transfer: str | None = None,
) -> QualitativeEvaluation:
    finding = []
    if action:
        finding = [
            MisconceptionFinding(
                concept_id="programming.invariants",
                description="Treats equal indices as always empty",
                evidence="The explanation collapses two distinct states",
                severity=4,
                action=action,
            )
        ]
    return QualitativeEvaluation(
        overall_score={"success": 90, "partial": 60, "failure": 25}[outcome],
        dimensions=[
            DimensionScore(dimension="correctness", score=80, rationale="Observed behavior"),
            DimensionScore(dimension="reasoning", score=75, rationale="Stated invariant"),
            DimensionScore(dimension="communication", score=75, rationale="Clear response"),
        ],
        grader_confidence=0.9,
        concept_evidence=[
            ConceptEvidence(
                concept_id="programming.invariants",
                outcome=outcome,
                strength=0.9,
                difficulty=4,
                exercise_type=exercise_type,
                rationale="Observed against the rubric",
                transfer_context=transfer,
            )
        ],
        misconceptions=finding,
        feedback_summary="Evidence was evaluated against the assignment rubric.",
        feedback_details=["One concrete observation."],
        classification="correct" if outcome == "success" else "incomplete",
        follow_up="new_assignment",
        follow_up_reason="Collect another context.",
        escalation_recommended=False,
    )


def persist_and_apply(
    database: Database,
    model: LearnerModel,
    value: QualitativeEvaluation,
    *,
    confidence: int,
    assignment_id: str = "A-0001",
) -> tuple[str, str]:
    attempt_id = str(uuid.uuid4())
    evaluation_id = str(uuid.uuid4())
    now = iso_now()
    database.execute(
        """
        INSERT INTO attempts(id, assignment_id, commit_sha, learner_confidence,
            submission_source, submitted_at)
        VALUES (?, ?, ?, ?, 'test', ?)
        """,
        (
            attempt_id,
            assignment_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            confidence,
            now,
        ),
    )
    database.execute(
        """
        INSERT INTO qualitative_evaluations(
            id, attempt_id, schema_version, evaluation_json, overall_score,
            grader_confidence, prompt_version, created_at
        ) VALUES (?, ?, '1.0', ?, ?, ?, 'test-v1', ?)
        """,
        (
            evaluation_id,
            attempt_id,
            value.model_dump_json(),
            value.overall_score,
            value.grader_confidence,
            now,
        ),
    )
    model.apply_evaluation(
        learner_id="learner",
        assignment_id=assignment_id,
        attempt_id=attempt_id,
        evaluation_id=evaluation_id,
        evaluation=value,
        learner_confidence=confidence,
    )
    return attempt_id, evaluation_id


def test_mastery_history_spacing_and_idempotency(initialized: tuple[Database, object]) -> None:
    database, _ = initialized
    setup_assignment(database)
    model = LearnerModel(database)
    value = evaluation(outcome="success", exercise_type=ExerciseType.DEBUGGING)
    attempt_id, evaluation_id = persist_and_apply(database, model, value, confidence=80)
    row = database.fetch_one(
        "SELECT * FROM mastery WHERE learner_id='learner' AND concept_id='programming.invariants'"
    )
    assert row is not None
    assert row["mastery_estimate"] > 0.2
    assert row["review_interval_days"] > 1
    assert row["highest_successful_difficulty"] == 4
    transition = database.fetch_one(
        "SELECT mastery_before, mastery_after FROM mastery_evidence WHERE attempt_id=?",
        (attempt_id,),
    )
    assert transition is not None
    assert transition["mastery_before"] == 0.2
    assert transition["mastery_after"] == row["mastery_estimate"]
    model.apply_evaluation(
        learner_id="learner",
        assignment_id="A-0001",
        attempt_id=attempt_id,
        evaluation_id=evaluation_id,
        evaluation=value,
        learner_confidence=80,
    )
    assert database.fetch_one(
        "SELECT COUNT(*) count FROM mastery_evidence WHERE attempt_id=?", (attempt_id,)
    ) == {"count": 1}
    persist_and_apply(
        database,
        model,
        evaluation(outcome="failure", exercise_type=ExerciseType.CODE_REVIEW),
        confidence=95,
    )
    failed = database.fetch_one(
        "SELECT * FROM mastery WHERE learner_id='learner' AND concept_id='programming.invariants'"
    )
    assert failed is not None
    assert failed["review_interval_days"] == 0.25
    assert model.calibration("learner")["observations"] == 2


def test_unassessed_domains_do_not_report_prior_as_readiness(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    model = LearnerModel(database)

    initial = model.readiness("learner", "systems-foundations")

    assert initial
    assert all(item["readiness"] is None for item in initial)
    assert all(item["uncertainty"] is None for item in initial)
    assert all(item["assessed_concept_count"] == 0 for item in initial)
    assert all(item["evidence_count"] == 0 for item in initial)

    assignment_id = setup_assignment(database)
    persist_and_apply(
        database,
        model,
        evaluation(outcome="success", exercise_type=ExerciseType.DEBUGGING),
        confidence=80,
        assignment_id=assignment_id,
    )
    programming = next(
        item
        for item in model.readiness("learner", "systems-foundations")
        if item["domain"] == "programming"
    )
    assert programming["readiness"] is not None
    assert programming["assessed_concept_count"] == 1
    assert programming["evidence_count"] == 1


def test_misconception_requires_transfer_then_can_recur(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    first_assignment = setup_assignment(database, ExerciseType.DEBUGGING)
    model = LearnerModel(database)
    initial_sequence: list[tuple[QualitativeEvaluation, int, str]] = [
        (
            evaluation(
                outcome="failure",
                exercise_type=ExerciseType.DEBUGGING,
                action="suspect",
            ),
            90,
            "suspected",
        ),
        (
            evaluation(
                outcome="failure",
                exercise_type=ExerciseType.DEBUGGING,
                action="confirm",
            ),
            70,
            "active",
        ),
        (
            evaluation(
                outcome="success",
                exercise_type=ExerciseType.DEBUGGING,
                action="resolve",
                transfer="the original queue repair",
            ),
            80,
            "active",
        ),
    ]
    for value, confidence, status in initial_sequence:
        persist_and_apply(
            database,
            model,
            value,
            confidence=confidence,
            assignment_id=first_assignment,
        )
        row = database.fetch_one("SELECT status FROM misconceptions")
        assert row == {"status": status}

    finish_assignment(database, first_assignment)
    challenge_assignment = setup_assignment(database, ExerciseType.EXPLAIN_CODE)
    persist_and_apply(
        database,
        model,
        evaluation(
            outcome="partial",
            exercise_type=ExerciseType.EXPLAIN_CODE,
            action="challenge",
            transfer="explaining the queue transition invariant",
        ),
        confidence=60,
        assignment_id=challenge_assignment,
    )
    assert database.fetch_one("SELECT status FROM misconceptions") == {
        "status": "challenged"
    }
    persist_and_apply(
        database,
        model,
        evaluation(
            outcome="success",
            exercise_type=ExerciseType.EXPLAIN_CODE,
            action="resolve",
            transfer="explaining the queue transition invariant",
        ),
        confidence=80,
        assignment_id=challenge_assignment,
    )
    assert database.fetch_one("SELECT status FROM misconceptions") == {
        "status": "challenged"
    }

    finish_assignment(database, challenge_assignment)
    transfer_assignment = setup_assignment(database, ExerciseType.IMPLEMENTATION)
    persist_and_apply(
        database,
        model,
        evaluation(
            outcome="success",
            exercise_type=ExerciseType.IMPLEMENTATION,
            action="resolve",
            transfer="implementing a framed parser state machine",
        ),
        confidence=85,
        assignment_id=transfer_assignment,
    )
    assert database.fetch_one("SELECT status FROM misconceptions") == {"status": "resolved"}

    finish_assignment(database, transfer_assignment)
    recurrence_assignment = setup_assignment(database, ExerciseType.DEBUGGING)
    persist_and_apply(
        database,
        model,
        evaluation(outcome="failure", exercise_type=ExerciseType.DEBUGGING, action="recur"),
        confidence=90,
        assignment_id=recurrence_assignment,
    )
    assert database.fetch_one("SELECT status FROM misconceptions") == {"status": "recurred"}
