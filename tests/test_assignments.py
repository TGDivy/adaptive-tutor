from __future__ import annotations

import json

import pytest

from adaptive_tutor.assignments import (
    AssignmentService,
    AssignmentValidator,
)
from adaptive_tutor.db import Database
from adaptive_tutor.errors import AssignmentValidationError, ConfigurationError
from adaptive_tutor.generation import CurriculumAssignmentGenerator
from adaptive_tutor.models import AssignmentRequest, CurriculumPackage, LearnerContext


def request() -> AssignmentRequest:
    return AssignmentRequest(
        learner_id="learner",
        curriculum_id="systems-foundations",
        profile_id="generalist",
        target_concepts=["programming.invariants"],
        target_difficulty=4,
        context=LearnerContext(available_minutes=45),
    )


def test_reference_solution_runs_against_public_and_hidden_harness(
    initialized: tuple[Database, CurriculumPackage],
) -> None:
    _, curriculum = initialized
    generator = CurriculumAssignmentGenerator(curriculum)
    bundle = generator.generate(request())
    result = AssignmentValidator().validate(bundle, request())
    assert result.valid
    assert result.checks["reference"].startswith("trusted reference passes")


def test_assignment_persistence_hides_reference_material(
    initialized: tuple[Database, CurriculumPackage],
) -> None:
    database, curriculum = initialized
    bundle = CurriculumAssignmentGenerator(curriculum).generate(request())
    validation = AssignmentValidator().validate(bundle, request(), run_reference=False)
    service = AssignmentService(database)
    created = service.create(request(), bundle, validation)
    assert created["id"] == "A-0001"
    assert created["branch_name"] == "assignment/0001-bounded-work-queue"
    public = service.public_files("A-0001")
    assert "README.md" in public
    assert all(not path.startswith(("reference/", "evaluator/")) for path in public)
    metadata = json.loads(public[".adaptive-tutor/assignment.json"])
    assert metadata == {
        "schema_version": "1.0",
        "id": "A-0001",
        "slug": "bounded-work-queue",
        "concepts": ["programming.invariants"],
        "exercise_type": "implementation",
        "difficulty": 4,
        "expected_minutes": 45,
        "current_stage": 1,
        "selection_reason": "",
        "tags": [
            "boundary-cases",
            "implementation",
            "programming.invariants",
            "python",
            "state-machines",
        ],
    }
    assert "hidden_evaluator" not in public[".adaptive-tutor/assignment.json"]
    assert service.active("learner") is not None
    with pytest.raises(ConfigurationError, match="already active"):
        service.create(request(), bundle, validation)


def test_progressive_hints_and_stages(
    initialized: tuple[Database, CurriculumPackage],
) -> None:
    database, curriculum = initialized
    bundle = CurriculumAssignmentGenerator(curriculum).generate(request())
    validation = AssignmentValidator().validate(bundle, request(), run_reference=False)
    service = AssignmentService(database)
    service.create(request(), bundle, validation)
    hints = [service.next_hint("A-0001", "learner") for _ in range(6)]
    assert [level for level, _ in hints] == [1, 2, 3, 4, 5, 5]
    assert service.unlock_follow_up("A-0001") == 2
    assert service.unlock_follow_up("A-0001") is None


def test_generator_avoids_and_validator_rejects_recent_problem(
    initialized: tuple[Database, CurriculumPackage],
) -> None:
    _, curriculum = initialized
    recent_request = request().model_copy(
        update={"recent_assignments": [{"slug": "bounded-work-queue"}]}
    )
    bundle = CurriculumAssignmentGenerator(curriculum).generate(recent_request)
    assert bundle.slug == "rolling-event-counter"
    assert AssignmentValidator().validate(
        bundle, recent_request, run_reference=False
    ).valid
    repeated = bundle.model_copy(update={"slug": "bounded-work-queue"})
    with pytest.raises(AssignmentValidationError, match="repeats"):
        AssignmentValidator().validate(repeated, recent_request, run_reference=False)
