"""Read models for the CLI, dashboard, reports, and personal-agent API."""

from __future__ import annotations

import json
from typing import Any

from .db import Database
from .learner import LearnerModel
from .models import ReadinessDomain, RuntimeStatus
from .time import iso_now


class StatusService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_status(self, learner_id: str, curriculum_id: str) -> RuntimeStatus:
        active = self.database.fetch_one(
            """
            SELECT id, title, status, difficulty, exercise_type, expected_minutes,
                   branch_name, pull_number, current_stage, created_at, updated_at,
                   publication_attempted_at, publication_error,
                   json_extract(bundle_json, '$.summary') summary,
                   json_extract(bundle_json, '$.selection_reason') selection_reason,
                   (SELECT ac.concept_id FROM assignment_concepts ac
                    WHERE ac.assignment_id=assignments.id AND ac.is_primary=1)
                       primary_concept_id
            FROM assignments WHERE learner_id=? AND status IN
                ('validated','published','submitted','reviewing','follow_up')
            ORDER BY created_at DESC LIMIT 1
            """,
            (learner_id,),
        )
        readiness = [
            ReadinessDomain.model_validate(item)
            for item in LearnerModel(self.database).readiness(learner_id, curriculum_id)
        ]
        weaknesses = self.database.fetch_all(
            """
            SELECT c.id concept_id, c.name, c.domain, m.mastery_estimate,
                   m.uncertainty, m.evidence_count, m.trend, m.next_review
            FROM mastery m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND c.curriculum_id=? AND m.evidence_count > 0
            ORDER BY m.mastery_estimate ASC, m.uncertainty DESC,
                     c.importance DESC LIMIT 6
            """,
            (learner_id, curriculum_id),
        )
        misconceptions = self.database.fetch_all(
            """
            SELECT m.id, m.concept_id, c.name concept_name, m.description,
                   m.status, m.frequency, m.severity, m.last_observed,
                   m.challenged_at, m.resolved_at, m.resolution_transfer_context
            FROM misconceptions m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND m.status IN
                ('suspected','active','challenged','recurred')
            ORDER BY m.severity DESC, m.frequency DESC, m.last_observed DESC LIMIT 8
            """,
            (learner_id,),
        )
        for item in misconceptions:
            item["lifecycle"] = self.database.fetch_all(
                """
                SELECT action, exercise_type, transfer_context, observed_at
                FROM misconception_evidence
                WHERE misconception_id=? ORDER BY observed_at, rowid
                """,
                (item["id"],),
            )
        reviews = self.database.fetch_all(
            """
            SELECT c.id concept_id, c.name, c.domain, m.next_review,
                   m.last_reviewed, m.review_interval_days, m.mastery_estimate,
                   CASE WHEN m.next_review <= ? THEN 1 ELSE 0 END due,
                   CAST(MAX(0, julianday(?) - julianday(m.next_review)) AS INTEGER)
                       overdue_days
            FROM mastery m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND c.curriculum_id=? AND m.next_review IS NOT NULL
            ORDER BY m.next_review ASC LIMIT 10
            """,
            (iso_now(), iso_now(), learner_id, curriculum_id),
        )
        scores = self.database.fetch_all(
            """
            SELECT a.id assignment_id, a.title, q.overall_score,
                   q.grader_confidence, q.review_kind, q.created_at
            FROM qualitative_evaluations q
            JOIN attempts at ON at.id=q.attempt_id
            JOIN assignments a ON a.id=at.assignment_id
            WHERE a.learner_id=? ORDER BY q.created_at DESC LIMIT 8
            """,
            (learner_id,),
        )
        activity = self.database.fetch_all(
            """
            SELECT kind, summary, occurred_at FROM activity
            WHERE learner_id=? ORDER BY occurred_at DESC LIMIT 12
            """,
            (learner_id,),
        )
        changes = self.database.fetch_all(
            """
            SELECT c.id concept_id, c.name, c.domain, a.title assignment_title,
                   e.mastery_before, e.mastery_after,
                   ROUND(e.mastery_after-e.mastery_before, 4) movement,
                   e.observed_at
            FROM mastery_evidence e
            JOIN concepts c ON c.id=e.concept_id
            LEFT JOIN assignments a ON a.id=e.assignment_id
            WHERE e.learner_id=? AND e.mastery_before IS NOT NULL
              AND e.mastery_after != e.mastery_before
            ORDER BY e.observed_at DESC LIMIT 6
            """,
            (learner_id,),
        )
        calibration = LearnerModel(self.database).calibration(learner_id)
        usage = self.database.fetch_one(
            """
            SELECT COUNT(*) invocations,
                   COALESCE(SUM(input_tokens), 0) input_tokens,
                   COALESCE(SUM(output_tokens), 0) output_tokens,
                   ROUND(COALESCE(SUM(cost_usd), 0), 6) cost_usd
            FROM model_invocations
            """
        ) or {"invocations": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}
        return RuntimeStatus(
            paused=self.is_paused(),
            active_curriculum=curriculum_id,
            active_assignment=active,
            readiness=readiness,
            weaknesses=weaknesses,
            misconceptions=misconceptions,
            upcoming_reviews=reviews,
            recent_scores=scores,
            recent_changes=changes,
            recent_activity=activity,
            confidence_calibration=calibration,
            model_usage=usage,
        )

    def is_paused(self) -> bool:
        row = self.database.fetch_one("SELECT value_json FROM configuration WHERE key='paused'")
        return bool(json.loads(row["value_json"])) if row else False

    def set_paused(self, paused: bool) -> None:
        self.database.execute(
            """
            INSERT INTO configuration(key, value_json, updated_at) VALUES ('paused', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (json.dumps(paused), iso_now()),
        )

    def history(self, learner_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT a.id, a.title, a.exercise_type, a.difficulty, a.status,
                   a.created_at, a.completed_at, MAX(q.overall_score) score,
                   COUNT(DISTINCT at.id) attempts
            FROM assignments a
            LEFT JOIN attempts at ON at.assignment_id=a.id
            LEFT JOIN qualitative_evaluations q ON q.attempt_id=at.id
            WHERE a.learner_id=? GROUP BY a.id
            ORDER BY a.created_at DESC LIMIT ?
            """,
            (learner_id, limit),
        )

    def concepts(self, learner_id: str, curriculum_id: str) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            """
            SELECT c.id, c.name, c.domain, c.importance, c.base_difficulty,
                   m.mastery_estimate, m.uncertainty, m.evidence_count,
                   m.successful_attempts, m.failed_attempts,
                   m.highest_successful_difficulty, m.recent_performance,
                   m.long_term_performance, m.next_review,
                   m.confidence_calibration, m.trend
            FROM concepts c JOIN mastery m ON m.concept_id=c.id
            WHERE m.learner_id=? AND c.curriculum_id=?
            ORDER BY c.domain, c.name
            """,
            (learner_id, curriculum_id),
        )
