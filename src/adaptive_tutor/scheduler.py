"""Explainable adaptive scheduling and difficulty/format selection."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .models import ExerciseType, LearnerContext, SchedulerCandidate
from .time import parse_time, utc_now


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class AdaptiveScheduler:
    def __init__(self, database: Database) -> None:
        self.database = database

    def recommend(
        self,
        learner_id: str,
        curriculum_id: str,
        profile_id: str,
        context: LearnerContext,
        *,
        now: datetime | None = None,
        limit: int = 5,
    ) -> list[SchedulerCandidate]:
        current = now or utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        profile_row = self.database.fetch_one(
            """
            SELECT domain_weights_json, concept_weights_json FROM profiles
            WHERE curriculum_id=? AND id=?
            """,
            (curriculum_id, profile_id),
        )
        if profile_row is None:
            raise ValueError(f"Unknown curriculum profile: {curriculum_id}/{profile_id}")
        domain_weights = json.loads(profile_row["domain_weights_json"])
        concept_weights = json.loads(profile_row["concept_weights_json"])
        rows = self.database.fetch_all(
            """
            SELECT c.*, m.mastery_estimate, m.uncertainty, m.evidence_count,
                   m.successful_attempts, m.failed_attempts,
                   m.highest_successful_difficulty, m.recent_performance,
                   m.long_term_performance, m.last_reviewed, m.next_review,
                   m.review_interval_days, m.confidence_calibration, m.trend
            FROM concepts c
            JOIN mastery m ON m.concept_id=c.id AND m.learner_id=?
            WHERE c.curriculum_id=?
            """,
            (learner_id, curriculum_id),
        )
        misconceptions = {
            row["concept_id"]: row
            for row in self.database.fetch_all(
                """
                SELECT concept_id, MAX(severity) severity, SUM(frequency) frequency,
                       GROUP_CONCAT(status) statuses
                FROM misconceptions
                WHERE learner_id=? AND status IN ('suspected','active','challenged','recurred')
                GROUP BY concept_id
                """,
                (learner_id,),
            )
        }
        recent = self.database.fetch_all(
            """
            SELECT concept_id, exercise_type, outcome, learner_confidence, observed_at
            FROM mastery_evidence WHERE learner_id=?
            ORDER BY observed_at DESC LIMIT 24
            """,
            (learner_id,),
        )
        concept_recency = Counter(item["concept_id"] for item in recent[:10])
        format_recency = Counter(item["exercise_type"] for item in recent[:10])
        last_by_concept: dict[str, dict[str, Any]] = {}
        for item in recent:
            last_by_concept.setdefault(item["concept_id"], item)
        prerequisite_rows = self.database.fetch_all(
            """
            SELECT r.concept_id, r.prerequisite_id,
                   COALESCE(m.mastery_estimate, 0.2) prerequisite_mastery
            FROM concept_relationships r
            LEFT JOIN mastery m ON m.concept_id=r.prerequisite_id AND m.learner_id=?
            WHERE r.curriculum_id=?
            """,
            (learner_id, curriculum_id),
        )
        prerequisite_mastery: dict[str, list[tuple[str, float]]] = defaultdict(list)
        blocked_dependents: dict[str, int] = Counter()
        for item in prerequisite_rows:
            mastery = float(item["prerequisite_mastery"])
            prerequisite_mastery[item["concept_id"]].append((item["prerequisite_id"], mastery))
            if mastery < 0.45:
                blocked_dependents[item["prerequisite_id"]] += 1

        candidates: list[SchedulerCandidate] = []
        for row in rows:
            concept_id = str(row["id"])
            mastery = float(row["mastery_estimate"])
            uncertainty_value = float(row["uncertainty"])
            importance = _clamp(float(row["importance"]), 0.1, 2.0)
            weakness = _clamp(0.3 + (1.0 - mastery), 0.3, 1.3)
            forgetting = self._forgetting_factor(row, current)
            uncertainty = 0.65 + uncertainty_value
            misconception = misconceptions.get(concept_id)
            misconception_factor = 1.0
            if misconception:
                severity = float(misconception["severity"])
                frequency = float(misconception["frequency"])
                recurrence = 0.3 if "recurred" in str(misconception["statuses"]) else 0.0
                misconception_factor = 1.0 + severity * 0.16 + min(frequency, 4) * 0.08 + recurrence
            profile_factor = float(
                concept_weights.get(concept_id, domain_weights.get(row["domain"], 1.0))
            )
            repeated_concept = concept_recency[concept_id]
            diversity = _clamp(1.0 - repeated_concept * 0.16, 0.42, 1.0)
            confidence = self._confidence_factor(last_by_concept.get(concept_id))
            prerequisites = prerequisite_mastery.get(concept_id, [])
            weakest_prerequisite = min((score for _, score in prerequisites), default=1.0)
            prerequisite = 1.0
            if weakest_prerequisite < 0.45:
                prerequisite = 0.62
            prerequisite *= 1.0 + min(blocked_dependents.get(concept_id, 0), 3) * 0.18
            urgency = 1.0
            if context.days_until_goal is not None:
                urgency += _clamp((30 - context.days_until_goal) / 100, 0, 0.3)
            priority = math.prod(
                (
                    importance,
                    weakness,
                    forgetting,
                    uncertainty,
                    misconception_factor,
                    profile_factor,
                    diversity,
                    confidence,
                    prerequisite,
                    urgency,
                )
            )
            exercise_type = self._select_exercise_type(
                json.loads(row["exercise_types_json"]),
                context.allowed_formats,
                format_recency,
                recent,
                concept_id,
            )
            difficulty = self._difficulty(row, last_by_concept.get(concept_id), context)
            factors = {
                "importance": round(importance, 3),
                "weakness": round(weakness, 3),
                "forgetting": round(forgetting, 3),
                "uncertainty": round(uncertainty, 3),
                "misconception": round(misconception_factor, 3),
                "profile": round(profile_factor, 3),
                "diversity": round(diversity, 3),
                "confidence": round(confidence, 3),
                "prerequisite": round(prerequisite, 3),
                "urgency": round(urgency, 3),
            }
            reasons = [f"mastery {mastery:.0%}", f"uncertainty {uncertainty_value:.0%}"]
            if forgetting > 1.1:
                reasons.append("review is due")
            if misconception:
                reasons.append("active misconception")
            if weakest_prerequisite < 0.45:
                reasons.append("a prerequisite needs reinforcement")
            if blocked_dependents.get(concept_id, 0):
                reasons.append("unblocks dependent concepts")
            candidates.append(
                SchedulerCandidate(
                    concept_id=concept_id,
                    exercise_type=exercise_type,
                    target_difficulty=difficulty,
                    priority=round(priority, 6),
                    factors=factors,
                    reason="; ".join(reasons),
                )
            )
        return sorted(candidates, key=lambda item: (-item.priority, item.concept_id))[:limit]

    @staticmethod
    def _forgetting_factor(row: dict[str, Any], now: datetime) -> float:
        last = parse_time(row.get("last_reviewed"))
        due = parse_time(row.get("next_review"))
        if last is None or due is None:
            return 1.25
        interval_seconds = max((due - last).total_seconds(), 86_400)
        elapsed = max((now - last).total_seconds(), 0)
        if now >= due:
            overdue = (now - due).total_seconds() / interval_seconds
            return _clamp(1.2 + overdue, 1.2, 2.5)
        return _clamp(0.65 + 0.55 * elapsed / interval_seconds, 0.65, 1.2)

    @staticmethod
    def _confidence_factor(last: dict[str, Any] | None) -> float:
        if not last or last.get("learner_confidence") is None:
            return 1.0
        confidence = int(last["learner_confidence"]) / 100
        outcome = last.get("outcome")
        if outcome == "failure":
            return 1.0 + 0.9 * confidence
        if outcome == "partial" and confidence >= 0.75:
            return 1.25
        return 0.9 + 0.1 * (1 - confidence)

    @staticmethod
    def _select_exercise_type(
        supported: list[str],
        allowed: list[ExerciseType],
        recent_counts: Counter[str],
        recent: list[dict[str, Any]],
        concept_id: str,
    ) -> ExerciseType:
        options = [item for item in allowed if item.value in supported]
        if not options:
            options = [ExerciseType(item) for item in supported]
        last_for_concept = next(
            (item["exercise_type"] for item in recent if item["concept_id"] == concept_id), None
        )
        return min(
            options,
            key=lambda item: (
                item.value == last_for_concept,
                recent_counts[item.value],
                list(ExerciseType).index(item),
            ),
        )

    @staticmethod
    def _difficulty(
        row: dict[str, Any],
        last: dict[str, Any] | None,
        context: LearnerContext,
    ) -> int:
        base = int(row["base_difficulty"])
        mastery = float(row["mastery_estimate"])
        highest = int(row["highest_successful_difficulty"])
        successes = int(row["successful_attempts"])
        failures = int(row["failed_attempts"])
        target = max(base, highest or 1)
        if mastery >= 0.8 and successes >= 2:
            target += 2
        elif mastery >= 0.6:
            target += 1
        elif mastery < 0.3 and failures:
            target -= 1
        if last and last.get("outcome") == "failure":
            target -= 1
        elif last and last.get("outcome") == "success" and mastery >= 0.65:
            target += 1
        if context.energy == "low" or context.available_minutes < 25:
            target -= 1
        elif context.energy == "high" and context.available_minutes >= 60:
            target += 1
        return int(_clamp(target, 1, 10))
