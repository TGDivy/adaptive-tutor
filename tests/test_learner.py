from __future__ import annotations

import uuid

from adaptive_tutor.assignments import (
    AssignmentService,
    AssignmentValidator,
    TemplateAssignmentGenerator,
)
from adaptive_tutor.db import Database
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


def setup_assignment(database: Database) -> None:
    request = AssignmentRequest(
        learner_id="learner",
        curriculum_id="systems-foundations",
        profile_id="generalist",
        target_concepts=["programming.invariants"],
        target_difficulty=4,
        context=LearnerContext(),
    )
    bundle = TemplateAssignmentGenerator().generate(request)
    validation = AssignmentValidator().validate(bundle, request, run_reference=False)
    AssignmentService(database).create(request, bundle, validation)


def evaluation(
    *,
    outcome: str,
    exercise_type: ExerciseType,
    action: str | None = None,
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
) -> tuple[str, str]:
    attempt_id = str(uuid.uuid4())
    evaluation_id = str(uuid.uuid4())
    now = iso_now()
    database.execute(
        """
        INSERT INTO attempts(id, assignment_id, commit_sha, learner_confidence,
            submission_source, submitted_at)
        VALUES (?, 'A-0001', ?, ?, 'test', ?)
        """,
        (attempt_id, uuid.uuid4().hex + uuid.uuid4().hex, confidence, now),
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
        assignment_id="A-0001",
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


def test_misconception_requires_transfer_then_can_recur(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    setup_assignment(database)
    model = LearnerModel(database)
    sequence: list[tuple[QualitativeEvaluation, int]] = [
        (evaluation(outcome="failure", exercise_type=ExerciseType.DEBUGGING, action="suspect"), 90),
        (evaluation(outcome="failure", exercise_type=ExerciseType.DEBUGGING, action="confirm"), 70),
        (
            evaluation(
                outcome="partial", exercise_type=ExerciseType.CODE_REVIEW, action="challenge"
            ),
            60,
        ),
        (
            evaluation(
                outcome="success",
                exercise_type=ExerciseType.CODE_REVIEW,
                action="resolve",
                transfer="same review format",
            ),
            80,
        ),
    ]
    expected = ["suspected", "active", "challenged", "challenged"]
    for (value, confidence), status in zip(sequence, expected, strict=True):
        persist_and_apply(database, model, value, confidence=confidence)
        row = database.fetch_one("SELECT status FROM misconceptions")
        assert row == {"status": status}
    persist_and_apply(
        database,
        model,
        evaluation(
            outcome="success",
            exercise_type=ExerciseType.IMPLEMENTATION,
            action="resolve",
            transfer="a new implementation context",
        ),
        confidence=85,
    )
    assert database.fetch_one("SELECT status FROM misconceptions") == {"status": "resolved"}
    persist_and_apply(
        database,
        model,
        evaluation(outcome="failure", exercise_type=ExerciseType.WRITTEN, action="recur"),
        confidence=90,
    )
    assert database.fetch_one("SELECT status FROM misconceptions") == {"status": "recurred"}
