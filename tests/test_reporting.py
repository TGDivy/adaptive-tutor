from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from adaptive_tutor.assignments import (
    AssignmentService,
    AssignmentValidator,
    TemplateAssignmentGenerator,
)
from adaptive_tutor.db import Database
from adaptive_tutor.demo import run_demo
from adaptive_tutor.models import AssignmentRequest, LearnerContext
from adaptive_tutor.reporting import ReportService
from adaptive_tutor.time import iso_now, utc_now


def test_local_demo_covers_evaluation_state_and_reporting(tmp_path: Path) -> None:
    result = run_demo(tmp_path / "demo")

    assert result.assignment["id"] == "A-0001"
    assert len(result.automated_evidence["checks"]) == 3
    assert result.qualitative_evaluation["overall_score"] > 0
    assert result.report.data["study_activity"]["assignments"] == 1
    assert result.report.data["study_activity"]["attempts"] == 1
    assert result.report.data["mastery_movement"]
    assert result.report.data["retention"]["observations"] == 1
    assert Path(result.database_path).is_file()


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
        bundle = TemplateAssignmentGenerator().generate(request)
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
