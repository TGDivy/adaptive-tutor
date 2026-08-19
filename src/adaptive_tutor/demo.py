"""Credential-free deterministic end-to-end product demonstration."""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from .assignments import AssignmentService, AssignmentValidator, ValidationResult
from .codex import FixtureCodexRunner
from .curriculum import CurriculumLoader, bundled_curriculum_path
from .db import Database
from .evaluation import EvaluationService
from .generation import CurriculumAssignmentGenerator
from .models import (
    AssignmentBundle,
    AssignmentRequest,
    ConceptEvidence,
    CurriculumPackage,
    DimensionScore,
    ExerciseType,
    LearnerContext,
    MisconceptionFinding,
    QualitativeEvaluation,
    SchedulerCandidate,
)
from .reporting import ReportDocument, ReportService
from .runner import CredentialFreeEvaluator
from .scheduler import AdaptiveScheduler
from .state import StatusService
from .time import iso_now, utc_now


@dataclass(frozen=True)
class DemoAttempt:
    outcome: Literal["success", "partial", "failure"]
    confidence: int
    age_days: int
    solved: bool
    misconception_action: Literal[
        "suspect", "confirm", "challenge", "resolve", "recur"
    ] | None = None
    transfer_context: str | None = None
    misconception_description: str | None = None


@dataclass(frozen=True)
class DemoResult:
    database_path: str
    config_path: str | None
    workspace_path: str
    curriculum: str
    recommendation: dict[str, Any]
    assignment: dict[str, Any]
    journey: list[dict[str, Any]]
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
    database_path = root / "demo.sqlite3"
    if database_path.exists():
        raise ValueError(
            f"Demo state already exists at {database_path}; choose a new --keep directory"
        )

    try:
        now = utc_now()
        database = Database(database_path)
        database.migrate()
        package = CurriculumLoader().load(bundled_curriculum_path())
        CurriculumLoader().persist(package, database, "demo-learner")
        workspace_root = root / "workspace"
        journey: list[dict[str, Any]] = []

        invariant_description = (
            "Treats equal ring-buffer indices as proof that the queue is always empty"
        )
        first, first_bundle, _ = _create_assignment(
            database,
            package,
            concept_id="programming.invariants",
            exercise_type=ExerciseType.DEBUGGING,
            difficulty=4,
            reason="Establish a baseline on state invariants.",
        )
        first_id = str(first["id"])
        journey.append(
            _run_attempt(
                database,
                package,
                first_id,
                first_bundle,
                workspace_root,
                now,
                DemoAttempt(
                    outcome="failure",
                    confidence=92,
                    age_days=24,
                    solved=False,
                    misconception_action="suspect",
                    misconception_description=invariant_description,
                ),
            )
        )
        journey.append(
            _run_attempt(
                database,
                package,
                first_id,
                first_bundle,
                workspace_root,
                now,
                DemoAttempt(
                    outcome="failure",
                    confidence=78,
                    age_days=22,
                    solved=False,
                    misconception_action="confirm",
                    misconception_description=invariant_description,
                ),
            )
        )
        _complete_assignment(database, first_id, now - timedelta(days=21), now - timedelta(days=25))

        challenge, challenge_bundle, _ = _create_assignment(
            database,
            package,
            concept_id="programming.invariants",
            exercise_type=ExerciseType.EXPLAIN_CODE,
            difficulty=4,
            reason="Challenge the active invariant misconception from a second representation.",
        )
        challenge_id = str(challenge["id"])
        journey.append(
            _run_attempt(
                database,
                package,
                challenge_id,
                challenge_bundle,
                workspace_root,
                now,
                DemoAttempt(
                    outcome="partial",
                    confidence=61,
                    age_days=16,
                    solved=True,
                    misconception_action="challenge",
                    misconception_description=invariant_description,
                ),
            )
        )
        _complete_assignment(
            database, challenge_id, now - timedelta(days=15), now - timedelta(days=17)
        )

        transfer, transfer_bundle, _ = _create_assignment(
            database,
            package,
            concept_id="programming.invariants",
            exercise_type=ExerciseType.IMPLEMENTATION,
            difficulty=5,
            reason="Test the challenged invariant in a new implementation context.",
        )
        transfer_id = str(transfer["id"])
        journey.append(
            _run_attempt(
                database,
                package,
                transfer_id,
                transfer_bundle,
                workspace_root,
                now,
                DemoAttempt(
                    outcome="success",
                    confidence=76,
                    age_days=10,
                    solved=True,
                    misconception_action="resolve",
                    transfer_context="implementing a framed parser state machine",
                    misconception_description=invariant_description,
                ),
            )
        )
        _complete_assignment(
            database, transfer_id, now - timedelta(days=9), now - timedelta(days=11)
        )

        measurement, measurement_bundle, _ = _create_assignment(
            database,
            package,
            concept_id="performance.measurement",
            exercise_type=ExerciseType.PERFORMANCE,
            difficulty=6,
            reason="Probe measurement rigor at a higher difficulty.",
        )
        measurement_id = str(measurement["id"])
        journey.append(
            _run_attempt(
                database,
                package,
                measurement_id,
                measurement_bundle,
                workspace_root,
                now,
                DemoAttempt(
                    outcome="failure",
                    confidence=88,
                    age_days=6,
                    solved=False,
                    misconception_action="suspect",
                    misconception_description=(
                        "Treats one favorable benchmark sample as a stable performance result"
                    ),
                ),
            )
        )
        _complete_assignment(
            database, measurement_id, now - timedelta(days=5), now - timedelta(days=7)
        )

        framing, framing_bundle, _ = _create_assignment(
            database,
            package,
            concept_id="networking.protocol-framing",
            exercise_type=ExerciseType.CODE_REVIEW,
            difficulty=6,
            reason="Collect transfer evidence in a different domain and review format.",
        )
        framing_id = str(framing["id"])
        journey.append(
            _run_attempt(
                database,
                package,
                framing_id,
                framing_bundle,
                workspace_root,
                now,
                DemoAttempt(
                    outcome="success",
                    confidence=73,
                    age_days=3,
                    solved=True,
                    transfer_context="reviewing fragmented network input handling",
                ),
            )
        )
        _complete_assignment(
            database, framing_id, now - timedelta(days=2), now - timedelta(days=4)
        )

        recurrence, recurrence_bundle, _ = _create_assignment(
            database,
            package,
            concept_id="programming.invariants",
            exercise_type=ExerciseType.DEBUGGING,
            difficulty=6,
            reason="Retrieve the repaired invariant after a delay.",
        )
        recurrence_id = str(recurrence["id"])
        last_journey = _run_attempt(
            database,
            package,
            recurrence_id,
            recurrence_bundle,
            workspace_root,
            now,
            DemoAttempt(
                outcome="failure",
                confidence=91,
                age_days=1,
                solved=False,
                misconception_action="recur",
                misconception_description=invariant_description,
            ),
        )
        journey.append(last_journey)
        _complete_assignment(
            database,
            recurrence_id,
            now - timedelta(hours=20),
            now - timedelta(days=2),
        )

        _rebuild_review_dates(database)
        recommendation = AdaptiveScheduler(database).recommend(
            "demo-learner",
            package.metadata.id,
            package.metadata.default_profile,
            LearnerContext(available_minutes=45, energy="medium"),
            now=now,
            limit=1,
        )[0]
        request = _request_for_candidate(database, package, recommendation)
        active_bundle = CurriculumAssignmentGenerator(package).generate(request)
        validation = AssignmentValidator().validate(active_bundle, request)
        active = AssignmentService(database).create(request, active_bundle, validation)
        active_id = str(active["id"])
        active_workspace = workspace_root / active_id / "current"
        _write_demo_submission(active_bundle, active_workspace, solved=False, confidence=0)
        database.execute(
            """
            INSERT INTO configuration(key, value_json, updated_at)
            VALUES ('demo_workspace_path', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (json.dumps(str(active_workspace)), iso_now()),
        )

        report = ReportService(database).generate(
            "demo-learner", package.metadata.id, "weekly", end=now
        )
        snapshot = StatusService(database).get_status(
            "demo-learner", package.metadata.id
        ).model_dump(mode="json")
        config_path = _write_demo_config(root, database_path) if data_dir is not None else None
        return DemoResult(
            database_path=str(database.path),
            config_path=str(config_path) if config_path else None,
            workspace_path=str(workspace_root),
            curriculum=package.metadata.name,
            recommendation=recommendation.model_dump(mode="json"),
            assignment={
                "id": active_id,
                "title": active_bundle.title,
                "exercise_type": active_bundle.exercise_type.value,
                "difficulty": active_bundle.difficulty,
                "expected_minutes": active_bundle.expected_minutes,
                "branch": active["branch_name"],
                "workspace": str(active_workspace),
                "selection_reason": active_bundle.selection_reason,
            },
            journey=journey,
            validation_checks=validation.checks,
            automated_evidence=dict(last_journey["automated_evidence"]),
            qualitative_evaluation=dict(last_journey["qualitative_evaluation"]),
            status=snapshot,
            report=report,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()


def _create_assignment(
    database: Database,
    package: CurriculumPackage,
    *,
    concept_id: str,
    exercise_type: ExerciseType,
    difficulty: int,
    reason: str,
) -> tuple[dict[str, Any], AssignmentBundle, ValidationResult]:
    concept = next(item for item in package.concepts if item.id == concept_id)
    concept_state = database.fetch_one(
        "SELECT * FROM mastery WHERE learner_id='demo-learner' AND concept_id=?",
        (concept_id,),
    ) or {}
    request = AssignmentRequest(
        learner_id="demo-learner",
        curriculum_id=package.metadata.id,
        profile_id=package.metadata.default_profile,
        target_concepts=[concept_id],
        active_misconceptions=database.fetch_all(
            """
            SELECT concept_id, description, status, severity, frequency
            FROM misconceptions
            WHERE learner_id='demo-learner'
              AND status IN ('suspected','active','challenged','recurred')
            ORDER BY severity DESC, frequency DESC
            """
        ),
        target_difficulty=difficulty,
        context=LearnerContext(available_minutes=45, allowed_formats=[exercise_type]),
        trusted_references={
            name: (package.root / "references" / name).read_text(encoding="utf-8")
            for name in concept.reference_files
        },
        concept_state={concept_id: concept_state},
        selection_reason=reason,
    )
    bundle = CurriculumAssignmentGenerator(package).generate(request)
    validation = AssignmentValidator().validate(bundle, request)
    return AssignmentService(database).create(request, bundle, validation), bundle, validation


def _request_for_candidate(
    database: Database,
    package: CurriculumPackage,
    candidate: SchedulerCandidate,
) -> AssignmentRequest:
    concept = next(item for item in package.concepts if item.id == candidate.concept_id)
    recent = database.fetch_all(
        """
        SELECT a.slug, a.exercise_type, a.difficulty, a.created_at,
               json_extract(a.bundle_json, '$.generator_metadata.blueprint_id') blueprint_id,
               (SELECT ac.concept_id FROM assignment_concepts ac
                WHERE ac.assignment_id=a.id AND ac.is_primary=1) primary_concept
        FROM assignments a
        WHERE a.learner_id='demo-learner' ORDER BY a.created_at DESC LIMIT 8
        """
    )
    misconceptions = database.fetch_all(
        """
        SELECT concept_id, description, status, severity, frequency
        FROM misconceptions
        WHERE learner_id='demo-learner'
          AND status IN ('suspected','active','challenged','recurred')
        ORDER BY severity DESC, frequency DESC
        """
    )
    concept_state = database.fetch_one(
        """
        SELECT mastery_estimate, uncertainty, evidence_count,
               highest_successful_difficulty, recent_performance,
               long_term_performance, next_review, confidence_calibration, trend
        FROM mastery WHERE learner_id='demo-learner' AND concept_id=?
        """,
        (candidate.concept_id,),
    ) or {}
    return AssignmentRequest(
        learner_id="demo-learner",
        curriculum_id=package.metadata.id,
        profile_id=package.metadata.default_profile,
        target_concepts=[candidate.concept_id],
        active_misconceptions=misconceptions,
        recent_assignments=recent,
        target_difficulty=candidate.target_difficulty,
        context=LearnerContext(
            available_minutes=45,
            energy="medium",
            allowed_formats=[candidate.exercise_type],
        ),
        trusted_references={
            name: (package.root / "references" / name).read_text(encoding="utf-8")
            for name in concept.reference_files
        },
        concept_state={candidate.concept_id: concept_state},
        selection_reason=candidate.reason,
        scheduler_factors=candidate.factors,
    )


def _run_attempt(
    database: Database,
    package: CurriculumPackage,
    assignment_id: str,
    bundle: AssignmentBundle,
    workspace_root: Path,
    now: datetime,
    scenario: DemoAttempt,
) -> dict[str, Any]:
    attempt_number = int(
        (
            database.fetch_one("SELECT COUNT(*) count FROM attempts") or {"count": 0}
        )["count"]
    ) + 1
    attempt_id = str(uuid.uuid4())
    commit_sha = f"{attempt_number:040x}"
    observed = now - timedelta(days=scenario.age_days)
    workspace = workspace_root / assignment_id / f"attempt-{attempt_number}"
    _write_demo_submission(
        bundle,
        workspace,
        solved=scenario.solved,
        confidence=scenario.confidence,
    )
    database.execute(
        """
        INSERT INTO attempts(
            id, assignment_id, commit_sha, learner_confidence,
            submission_source, submitted_at
        ) VALUES (?, ?, ?, ?, 'local_demo', ?)
        """,
        (
            attempt_id,
            assignment_id,
            commit_sha,
            scenario.confidence,
            observed.isoformat(timespec="seconds"),
        ),
    )
    automated = CredentialFreeEvaluator().evaluate(
        bundle=bundle,
        assignment_id=assignment_id,
        commit_sha=commit_sha,
        workspace=workspace,
    )
    if automated.learner_passed != scenario.solved:
        expected = "pass" if scenario.solved else "fail"
        raise ValueError(f"Demo submission for {assignment_id} did not {expected} as designed")
    elapsed_ms = sum(item.duration_ms for item in automated.checks)
    automated = automated.model_copy(
        update={
            "started_at": observed,
            "completed_at": observed + timedelta(milliseconds=max(elapsed_ms, 1)),
        }
    ).with_computed_digest()
    qualitative_fixture = _qualitative_fixture(bundle, scenario)
    evaluator = EvaluationService(database, FixtureCodexRunner(qualitative_fixture))
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
            item.path: (workspace / item.path).read_text(encoding="utf-8")
            for item in bundle.files
            if item.role == "starter" and (workspace / item.path).is_file()
        },
        trusted_instructions=package.prompts["grading"],
        prompt_version=package.metadata.version,
        learner_confidence=scenario.confidence,
    )
    database.execute(
        """
        UPDATE attempts SET outcome=?, failure_kind=?, submitted_at=? WHERE id=?
        """,
        (
            scenario.outcome,
            "learner" if scenario.outcome == "failure" else None,
            observed.isoformat(timespec="seconds"),
            attempt_id,
        ),
    )
    for statement in (
        "UPDATE automated_evaluations SET created_at=? WHERE attempt_id=?",
        "UPDATE qualitative_evaluations SET created_at=? WHERE attempt_id=?",
        "UPDATE mastery_evidence SET observed_at=? WHERE attempt_id=?",
        "UPDATE confidence_observations SET observed_at=? WHERE attempt_id=?",
        "UPDATE misconception_evidence SET observed_at=? WHERE attempt_id=?",
    ):
        database.execute(
            statement,
            (observed.isoformat(timespec="seconds"), attempt_id),
        )
    database.execute(
        """
        UPDATE activity SET occurred_at=?
        WHERE kind='evaluation_applied'
          AND json_extract(metadata_json, '$.attempt_id')=?
        """,
        (observed.isoformat(timespec="seconds"), attempt_id),
    )
    database.execute(
        "UPDATE assignments SET head_sha=?, updated_at=? WHERE id=?",
        (commit_sha, observed.isoformat(timespec="seconds"), assignment_id),
    )
    return {
        "assignment_id": assignment_id,
        "title": bundle.title,
        "exercise_type": bundle.exercise_type.value,
        "difficulty": bundle.difficulty,
        "outcome": scenario.outcome,
        "score": qualitative.overall_score,
        "confidence": scenario.confidence,
        "age_days": scenario.age_days,
        "workspace": str(workspace),
        "automated_passed": automated.learner_passed,
        "automated_evidence": automated.model_dump(mode="json"),
        "qualitative_evaluation": qualitative.model_dump(mode="json"),
    }


def _qualitative_fixture(
    bundle: AssignmentBundle,
    scenario: DemoAttempt,
) -> QualitativeEvaluation:
    scores = {
        "success": (92.0, 84.0, 82.0),
        "partial": (62.0, 55.0, 57.0),
        "failure": (20.0, 25.0, 27.0),
    }[scenario.outcome]
    overall = sum(scores) / len(scores)
    classifications = {
        "success": "correct",
        "partial": "incomplete",
        "failure": "wrong",
    }
    findings = []
    if scenario.misconception_action and scenario.misconception_description:
        findings.append(
            MisconceptionFinding(
                concept_id=bundle.concepts[0],
                description=scenario.misconception_description,
                evidence="The submitted reasoning repeats the relevant mistaken assumption.",
                severity=4,
                action=scenario.misconception_action,
            )
        )
    summary = {
        "success": "The solution passes the checks and explains the governing invariant clearly.",
        "partial": "The implementation works, but the explanation does not yet transfer the idea.",
        "failure": "The deterministic failure exposes a specific error in the underlying model.",
    }[scenario.outcome]
    return QualitativeEvaluation(
        overall_score=overall,
        dimensions=[
            DimensionScore(
                dimension="correctness",
                score=scores[0],
                rationale="Public and hidden deterministic evidence was checked.",
            ),
            DimensionScore(
                dimension="reasoning",
                score=scores[1],
                rationale="The explanation was compared with the trusted expectation.",
            ),
            DimensionScore(
                dimension="communication",
                score=scores[2],
                rationale="The response makes its assumptions and conclusion inspectable.",
            ),
        ],
        grader_confidence=0.93,
        concept_evidence=[
            ConceptEvidence(
                concept_id=bundle.concepts[0],
                outcome=scenario.outcome,
                strength=0.9,
                difficulty=bundle.difficulty,
                exercise_type=bundle.exercise_type,
                rationale=(
                    "The result is grounded in the assignment rubric and deterministic checks."
                ),
                transfer_context=scenario.transfer_context,
            )
        ],
        misconceptions=findings,
        feedback_summary=summary,
        feedback_details=[
            "The most important observation is tied to a concrete boundary case.",
            "The next assignment should test the same idea in a different representation.",
        ],
        classification=classifications[scenario.outcome],  # type: ignore[arg-type]
        follow_up="new_assignment",
        follow_up_reason="Collect another observation with a different format or context.",
        escalation_recommended=False,
    )


def _complete_assignment(
    database: Database,
    assignment_id: str,
    completed_at: datetime,
    created_at: datetime,
) -> None:
    created_text = created_at.isoformat(timespec="seconds")
    completed_text = completed_at.isoformat(timespec="seconds")
    database.execute(
        """
        UPDATE assignments SET status='completed', created_at=?, completed_at=?, updated_at=?
        WHERE id=?
        """,
        (created_text, completed_text, completed_text, assignment_id),
    )
    database.execute(
        """
        UPDATE activity SET occurred_at=?
        WHERE kind='assignment_created'
          AND json_extract(metadata_json, '$.assignment_id')=?
        """,
        (created_text, assignment_id),
    )


def _rebuild_review_dates(database: Database) -> None:
    rows = database.fetch_all(
        """
        SELECT m.concept_id, m.review_interval_days, MAX(e.observed_at) last_observed
        FROM mastery m JOIN mastery_evidence e
          ON e.learner_id=m.learner_id AND e.concept_id=m.concept_id
        WHERE m.learner_id='demo-learner'
        GROUP BY m.concept_id, m.review_interval_days
        """
    )
    for row in rows:
        last = datetime.fromisoformat(str(row["last_observed"]))
        next_review = last + timedelta(days=float(row["review_interval_days"]))
        database.execute(
            """
            UPDATE mastery SET last_reviewed=?, next_review=?, updated_at=?
            WHERE learner_id='demo-learner' AND concept_id=?
            """,
            (
                last.isoformat(timespec="seconds"),
                next_review.isoformat(timespec="seconds"),
                last.isoformat(timespec="seconds"),
                row["concept_id"],
            ),
        )
    for misconception in database.fetch_all(
        "SELECT id FROM misconceptions WHERE learner_id='demo-learner'"
    ):
        evidence = database.fetch_all(
            """
            SELECT action, transfer_context, observed_at FROM misconception_evidence
            WHERE misconception_id=? ORDER BY observed_at, rowid
            """,
            (misconception["id"],),
        )
        challenged = next(
            (item["observed_at"] for item in evidence if item["action"] == "challenge"), None
        )
        resolved = next(
            (item["observed_at"] for item in evidence if item["action"] == "resolve"), None
        )
        database.execute(
            """
            UPDATE misconceptions SET first_observed=?, last_observed=?,
                challenged_at=?, resolved_at=? WHERE id=?
            """,
            (
                evidence[0]["observed_at"],
                evidence[-1]["observed_at"],
                challenged,
                resolved,
                misconception["id"],
            ),
        )


def _write_demo_submission(
    bundle: AssignmentBundle,
    workspace: Path,
    *,
    solved: bool,
    confidence: int,
) -> None:
    by_path = {item.path: item for item in bundle.files}
    replacements = bundle.hidden_evaluator.get("reference_replacements", {})
    replacement_by_target = {
        str(target): by_path[str(source)].content
        for target, source in replacements.items()
        if solved and str(source) in by_path
    }
    for item in bundle.files:
        if item.role not in {"instructions", "starter", "public_test"}:
            continue
        target = workspace / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        content = replacement_by_target.get(item.path, item.content)
        if item.path.endswith(("ANSWER.md", "REVIEW.md", "RESPONSE.md", "ANALYSIS.md")):
            content = (
                "# Analysis\n\nAn explicit occupancy value preserves the empty/full "
                "invariant through wraparound, and the regression exercises repeated fill "
                "and drain cycles.\n\n"
                if solved
                else "# Analysis\n\nEqual indices mean the structure is empty in every state.\n\n"
            ) + f"Confidence: {confidence}\n"
        target.write_text(content, encoding="utf-8")


def _write_demo_config(root: Path, database_path: Path) -> Path:
    config_path = root / "config.yaml"
    payload = {
        "data_dir": str(root),
        "database_path": str(database_path),
        "active_curriculum": "systems-foundations",
        "active_profile": "generalist",
        "learner_id": "demo-learner",
        "github": {"owner": ""},
        "codex": {"enabled": False},
        "server": {
            "host": "127.0.0.1",
            "port": 8765,
            "allow_unauthenticated_loopback": True,
        },
    }
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path
