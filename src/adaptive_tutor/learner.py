"""Transactional learner-state, confidence, and misconception updates."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import timedelta
from typing import Any

from .db import Database
from .models import (
    ConceptEvidence,
    MisconceptionFinding,
    MisconceptionStatus,
    QualitativeEvaluation,
)
from .time import utc_now

OUTCOME_VALUE = {"success": 1.0, "partial": 0.5, "failure": 0.0}


class LearnerModel:
    def __init__(self, database: Database) -> None:
        self.database = database

    def apply_evaluation(
        self,
        *,
        learner_id: str,
        assignment_id: str,
        attempt_id: str,
        evaluation_id: str,
        evaluation: QualitativeEvaluation,
        learner_confidence: int | None,
        source: str = "qualitative_evaluation",
    ) -> None:
        """Apply already schema-validated evidence in one all-or-nothing transaction."""
        observed = utc_now()
        observed_at = observed.isoformat(timespec="seconds")
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM qualitative_evaluations WHERE id=?", (evaluation_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("Evaluation must be persisted before updating learner state")
            already = connection.execute(
                """
                SELECT 1 FROM mastery_evidence
                WHERE attempt_id=? AND source=? LIMIT 1
                """,
                (attempt_id, source),
            ).fetchone()
            if already:
                return
            evidence_by_concept = {
                evidence.concept_id: evidence
                for evidence in evaluation.concept_evidence
                if evidence.outcome != "not_observed"
            }
            for evidence in evidence_by_concept.values():
                self._apply_concept_evidence(
                    connection,
                    learner_id,
                    assignment_id,
                    attempt_id,
                    evidence,
                    learner_confidence,
                    source,
                    observed,
                )
            for finding in evaluation.misconceptions:
                self._apply_misconception(
                    connection,
                    learner_id,
                    assignment_id,
                    attempt_id,
                    finding,
                    evidence_by_concept.get(finding.concept_id),
                    learner_confidence,
                    observed_at,
                )
            connection.execute(
                """
                INSERT INTO activity(id, learner_id, kind, summary, metadata_json, occurred_at)
                VALUES (?, ?, 'evaluation_applied', ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    learner_id,
                    f"Applied evaluation for {assignment_id}: {evaluation.overall_score:.0f}/100",
                    json.dumps(
                        {
                            "assignment_id": assignment_id,
                            "attempt_id": attempt_id,
                            "evaluation_id": evaluation_id,
                        },
                        sort_keys=True,
                    ),
                    observed_at,
                ),
            )

    def _apply_concept_evidence(
        self,
        connection: Any,
        learner_id: str,
        assignment_id: str,
        attempt_id: str,
        evidence: ConceptEvidence,
        confidence: int | None,
        source: str,
        observed: Any,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM mastery WHERE learner_id=? AND concept_id=?",
            (learner_id, evidence.concept_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"No mastery row for concept {evidence.concept_id}")
        outcome = OUTCOME_VALUE[evidence.outcome]
        count = int(row["evidence_count"])
        old_mastery = float(row["mastery_estimate"])
        learning_rate = max(0.08, 0.34 / math.sqrt(count + 1)) * evidence.strength
        new_mastery = old_mastery + learning_rate * (outcome - old_mastery)
        new_mastery = min(1.0, max(0.0, new_mastery))
        success = evidence.outcome == "success"
        failure = evidence.outcome == "failure"
        uncertainty = max(0.08, float(row["uncertainty"]) * 0.86)
        if evidence.outcome == "partial":
            uncertainty = min(1.0, uncertainty + 0.04)
        interval = max(float(row["review_interval_days"]), 0.25)
        confidence_fraction = confidence / 100 if confidence is not None else 0.5
        if success:
            interval = min(180.0, max(1.0, interval * (1.8 + 0.8 * new_mastery)))
        elif failure:
            interval = 0.25 if confidence_fraction >= 0.75 else 0.75
        else:
            interval = max(0.75, interval * 0.65)
        next_review = observed + timedelta(days=interval)
        connection.execute(
            """
            INSERT INTO mastery_evidence(
                id, learner_id, concept_id, assignment_id, attempt_id, outcome,
                strength, difficulty, exercise_type, learner_confidence,
                transfer_context, source, observed_at, mastery_before, mastery_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                learner_id,
                evidence.concept_id,
                assignment_id,
                attempt_id,
                evidence.outcome,
                evidence.strength,
                evidence.difficulty,
                evidence.exercise_type.value,
                confidence,
                evidence.transfer_context,
                source,
                observed.isoformat(timespec="seconds"),
                old_mastery,
                new_mastery,
            ),
        )
        history = connection.execute(
            """
            SELECT outcome FROM mastery_evidence
            WHERE learner_id=? AND concept_id=? ORDER BY observed_at DESC LIMIT 12
            """,
            (learner_id, evidence.concept_id),
        ).fetchall()
        values = [OUTCOME_VALUE[item["outcome"]] for item in history]
        recent = sum(values[:5]) / len(values[:5])
        long_term = sum(values) / len(values)
        prior_recent = sum(values[5:10]) / len(values[5:10]) if values[5:10] else old_mastery
        trend = recent - prior_recent
        calibration = float(row["confidence_calibration"])
        if confidence is not None:
            error = abs(confidence_fraction - outcome)
            calibration_score = 1.0 - error
            calibration = (
                calibration * count + calibration_score
            ) / (count + 1)
            connection.execute(
                """
                INSERT INTO confidence_observations(
                    id, learner_id, concept_id, attempt_id, confidence,
                    correctness, calibration_error, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    learner_id,
                    evidence.concept_id,
                    attempt_id,
                    confidence,
                    outcome,
                    error,
                    observed.isoformat(timespec="seconds"),
                ),
            )
        highest = int(row["highest_successful_difficulty"])
        if success:
            highest = max(highest, evidence.difficulty)
        connection.execute(
            """
            UPDATE mastery SET
                mastery_estimate=?, uncertainty=?, evidence_count=evidence_count+1,
                successful_attempts=successful_attempts+?, failed_attempts=failed_attempts+?,
                highest_successful_difficulty=?, recent_performance=?,
                long_term_performance=?, last_reviewed=?, next_review=?,
                review_interval_days=?, confidence_calibration=?, trend=?, updated_at=?
            WHERE learner_id=? AND concept_id=?
            """,
            (
                new_mastery,
                uncertainty,
                int(success),
                int(failure),
                highest,
                recent,
                long_term,
                observed.isoformat(timespec="seconds"),
                next_review.isoformat(timespec="seconds"),
                interval,
                calibration,
                trend,
                observed.isoformat(timespec="seconds"),
                learner_id,
                evidence.concept_id,
            ),
        )

    def _apply_misconception(
        self,
        connection: Any,
        learner_id: str,
        assignment_id: str,
        attempt_id: str,
        finding: MisconceptionFinding,
        concept_evidence: ConceptEvidence | None,
        confidence: int | None,
        observed_at: str,
    ) -> None:
        normalized = re.sub(r"\W+", " ", finding.description.lower()).strip()
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:20]
        existing = connection.execute(
            """
            SELECT * FROM misconceptions
            WHERE learner_id=? AND concept_id=? AND fingerprint=?
            """,
            (learner_id, finding.concept_id, fingerprint),
        ).fetchone()
        if existing is None:
            misconception_id = str(uuid.uuid4())
            initial = (
                MisconceptionStatus.ACTIVE
                if finding.action == "confirm"
                else MisconceptionStatus.SUSPECTED
            )
            connection.execute(
                """
                INSERT INTO misconceptions(
                    id, learner_id, concept_id, fingerprint, description, status,
                    first_observed, last_observed, frequency, severity, learner_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    misconception_id,
                    learner_id,
                    finding.concept_id,
                    fingerprint,
                    finding.description,
                    initial.value,
                    observed_at,
                    observed_at,
                    finding.severity,
                    confidence,
                ),
            )
            status = initial
        else:
            misconception_id = str(existing["id"])
            current = MisconceptionStatus(existing["status"])
            status = self._next_misconception_status(
                connection, misconception_id, current, finding, concept_evidence
            )
            frequency = int(existing["frequency"]) + int(
                finding.action in {"suspect", "confirm", "recur"}
            )
            if current == MisconceptionStatus.SUSPECTED and frequency >= 2:
                status = MisconceptionStatus.ACTIVE
            connection.execute(
                """
                UPDATE misconceptions SET description=?, status=?, last_observed=?,
                    frequency=?, severity=MAX(severity, ?), learner_confidence=?,
                    challenged_at=CASE WHEN ?='challenged' THEN ? ELSE challenged_at END,
                    resolved_at=CASE WHEN ?='resolved' THEN ? ELSE resolved_at END,
                    resolution_transfer_context=CASE WHEN ?='resolved' THEN ?
                        ELSE resolution_transfer_context END
                WHERE id=?
                """,
                (
                    finding.description,
                    status.value,
                    observed_at,
                    frequency,
                    finding.severity,
                    confidence,
                    status.value,
                    observed_at,
                    status.value,
                    observed_at,
                    status.value,
                    concept_evidence.transfer_context if concept_evidence else None,
                    misconception_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO misconception_evidence(
                id, misconception_id, assignment_id, attempt_id, action, evidence,
                exercise_type, transfer_context, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                misconception_id,
                assignment_id,
                attempt_id,
                finding.action,
                finding.evidence,
                concept_evidence.exercise_type.value if concept_evidence else None,
                concept_evidence.transfer_context if concept_evidence else None,
                observed_at,
            ),
        )

    @staticmethod
    def _next_misconception_status(
        connection: Any,
        misconception_id: str,
        current: MisconceptionStatus,
        finding: MisconceptionFinding,
        evidence: ConceptEvidence | None,
    ) -> MisconceptionStatus:
        if finding.action == "recur" or (
            current == MisconceptionStatus.RESOLVED
            and finding.action in {"suspect", "confirm"}
        ):
            return MisconceptionStatus.RECURRED
        if finding.action == "confirm":
            return MisconceptionStatus.ACTIVE
        if finding.action == "challenge":
            return MisconceptionStatus.CHALLENGED
        if finding.action != "resolve":
            return current
        if (
            evidence is None
            or evidence.outcome != "success"
            or not evidence.transfer_context
        ):
            return MisconceptionStatus.CHALLENGED
        prior_formats = {
            row["exercise_type"]
            for row in connection.execute(
                """
                SELECT exercise_type FROM misconception_evidence
                WHERE misconception_id=? AND exercise_type IS NOT NULL
                """,
                (misconception_id,),
            ).fetchall()
        }
        if not prior_formats or evidence.exercise_type.value in prior_formats:
            return MisconceptionStatus.CHALLENGED
        return MisconceptionStatus.RESOLVED

    def readiness(self, learner_id: str, curriculum_id: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT c.domain,
                   ROUND(SUM(m.mastery_estimate * c.importance) / SUM(c.importance), 4)
                       AS readiness,
                   ROUND(SUM(m.uncertainty * c.importance) / SUM(c.importance), 4)
                       AS uncertainty,
                   COUNT(*) AS concept_count
            FROM mastery m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND c.curriculum_id=?
            GROUP BY c.domain ORDER BY c.domain
            """,
            (learner_id, curriculum_id),
        )

    def calibration(self, learner_id: str) -> dict[str, float | int]:
        row = self.database.fetch_one(
            """
            SELECT COUNT(*) observations,
                   COALESCE(AVG(calibration_error), 0) mean_absolute_error,
                   COALESCE(AVG(confidence), 0) mean_confidence,
                   COALESCE(AVG(correctness) * 100, 0) mean_correctness
            FROM confidence_observations WHERE learner_id=?
            """,
            (learner_id,),
        )
        return row or {
            "observations": 0,
            "mean_absolute_error": 0.0,
            "mean_confidence": 0.0,
            "mean_correctness": 0.0,
        }
