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

    def get_status(
        self, learner_id: str, curriculum_id: str
    ) -> RuntimeStatus:
        active = self.database.fetch_one(
            """
            SELECT id, title, status, difficulty, exercise_type, expected_minutes,
                   branch_name, pull_number, current_stage, created_at, updated_at
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
                   m.uncertainty, m.trend, m.next_review
            FROM mastery m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND c.curriculum_id=?
            ORDER BY m.mastery_estimate ASC, m.uncertainty DESC,
                     c.importance DESC LIMIT 6
            """,
            (learner_id, curriculum_id),
        )
        misconceptions = self.database.fetch_all(
            """
            SELECT m.id, m.concept_id, c.name concept_name, m.description,
                   m.status, m.frequency, m.severity, m.last_observed
            FROM misconceptions m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND m.status IN
                ('suspected','active','challenged','recurred')
            ORDER BY m.severity DESC, m.frequency DESC, m.last_observed DESC LIMIT 8
            """,
            (learner_id,),
        )
        reviews = self.database.fetch_all(
            """
            SELECT c.id concept_id, c.name, c.domain, m.next_review,
                   m.review_interval_days, m.mastery_estimate
            FROM mastery m JOIN concepts c ON c.id=m.concept_id
            WHERE m.learner_id=? AND c.curriculum_id=? AND m.next_review IS NOT NULL
            ORDER BY m.next_review ASC LIMIT 10
            """,
            (learner_id, curriculum_id),
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
            recent_activity=activity,
            model_usage=usage,
        )

    def is_paused(self) -> bool:
        row = self.database.fetch_one(
            "SELECT value_json FROM configuration WHERE key='paused'"
        )
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
