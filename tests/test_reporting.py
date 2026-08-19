from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from adaptive_tutor.assignments import (
    AssignmentService,
    AssignmentValidator,
)
from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.db import Database
from adaptive_tutor.demo import run_demo
from adaptive_tutor.generation import CurriculumAssignmentGenerator
from adaptive_tutor.models import AssignmentRequest, LearnerContext
from adaptive_tutor.reporting import ReportService
from adaptive_tutor.time import iso_now, utc_now


def test_local_demo_covers_evaluation_state_and_reporting(tmp_path: Path) -> None:
    result = run_demo(tmp_path / "demo")

    assert result.assignment["id"] == "A-0007"
    assert result.status["active_assignment"]["id"] == "A-0007"
    assert result.assignment["selection_reason"]
    assert len(result.journey) == 7
    assert {item["outcome"] for item in result.journey} == {
        "success",
        "partial",
        "failure",
    }
    assert len({item["exercise_type"] for item in result.journey}) >= 5
    assert sum(bool(item["automated_passed"]) for item in result.journey) == 3
    assert len(result.automated_evidence["checks"]) == 4
    assert {check["name"] for check in result.automated_evidence["checks"]} >= {
        "fixture evaluator",
        "public and hidden tests",
    }
    assert result.automated_evidence["runner"].startswith("adaptive-tutor-local-fixture:")
    assert result.qualitative_evaluation["overall_score"] > 0
    assert result.report.data["study_activity"]["assignments"] >= 3
    assert result.report.data["study_activity"]["attempts"] == 3
    assert result.report.data["mastery_movement"]
    assert result.report.data["retention"]["observations"] == 1
    assert result.report.data["retention"]["due_reviews"] == 3
    assert any(
        item["status"] == "recurred" for item in result.status["misconceptions"]
    )
    assert Path(result.database_path).is_file()
    assert result.config_path is not None and Path(result.config_path).is_file()
    assert Path(result.workspace_path, "A-0007", "current", "README.md").is_file()


def test_report_sums_equal_duration_assignments_without_deduplicating(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    service = AssignmentService(database)
    expected_minutes = 0
    for number in range(2):
        request = AssignmentRequest(
            learner_id="learner",
            curriculum_id="systems-foundations",
            profile_id="generalist",
            target_concepts=["programming.invariants"],
            target_difficulty=4,
            context=LearnerContext(available_minutes=45),
        )
        bundle = CurriculumAssignmentGenerator(
            CurriculumLoader().load(bundled_curriculum_path())
        ).generate(request)
        validation = AssignmentValidator().validate(bundle, request, run_reference=False)
        created = service.create(request, bundle, validation)
        expected_minutes += bundle.expected_minutes
        database.execute(
            "UPDATE assignments SET status='completed', completed_at=?, updated_at=? WHERE id=?",
            (iso_now(), iso_now(), created["id"]),
        )
        database.execute(
            """
            INSERT INTO attempts(
                id, assignment_id, commit_sha, submission_source, submitted_at
            ) VALUES (?, ?, ?, 'test', ?)
            """,
            (str(uuid.uuid4()), created["id"], f"{number + 1:040x}", iso_now()),
        )

    report = ReportService(database).generate(
        "learner",
        "systems-foundations",
        end=utc_now() + timedelta(seconds=1),
    )
    assert report.data["study_activity"] == {
        "assignments": 2,
        "planned_minutes": expected_minutes,
        "attempts": 2,
        "hints": 0,
    }


def test_report_hides_unassessed_extremes_and_counts_only_repeat_retrieval(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    now = utc_now()
    earlier = now - timedelta(days=2)
    later = now - timedelta(days=1)
    for observed, outcome in ((earlier, "failure"), (later, "success")):
        database.execute(
            """
            INSERT INTO mastery_evidence(
                id, learner_id, concept_id, outcome, strength, difficulty,
                exercise_type, source, observed_at
            ) VALUES (?, 'learner', 'programming.invariants', ?, 1, 4,
                      'debugging', 'test', ?)
            """,
            (str(uuid.uuid4()), outcome, observed.isoformat(timespec="seconds")),
        )
    database.execute(
        """
        UPDATE mastery SET evidence_count=2, mastery_estimate=0.6, uncertainty=0.4
        WHERE learner_id='learner' AND concept_id='programming.invariants'
        """
    )

    report = ReportService(database).generate(
        "learner", "systems-foundations", end=now + timedelta(seconds=1)
    )

    assert [item["concept_id"] for item in report.data["strengths"]] == [
        "programming.invariants"
    ]
    assert [item["concept_id"] for item in report.data["weaknesses"]] == [
        "programming.invariants"
    ]
    assert report.data["retention"] == {
        "observations": 1,
        "successes": 1,
        "failures": 0,
        "due_reviews": 0,
    }
