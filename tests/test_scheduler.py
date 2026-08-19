from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from adaptive_tutor.db import Database
from adaptive_tutor.models import ExerciseType, LearnerContext
from adaptive_tutor.scheduler import AdaptiveScheduler


def test_scheduler_exposes_every_required_factor(initialized: tuple[Database, object]) -> None:
    database, _ = initialized
    recommendations = AdaptiveScheduler(database).recommend(
        "learner",
        "systems-foundations",
        "generalist",
        LearnerContext(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert recommendations
    assert set(recommendations[0].factors) == {
        "importance",
        "weakness",
        "forgetting",
        "uncertainty",
        "misconception",
        "profile",
        "diversity",
        "confidence",
        "prerequisite",
        "urgency",
    }
    assert "unblocks dependent concepts" in recommendations[0].reason


def test_confident_failure_is_prioritized_and_shortens_difficulty(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    now = datetime(2026, 2, 1, tzinfo=UTC)
    database.execute(
        """
        INSERT INTO mastery_evidence(
            id, learner_id, concept_id, outcome, strength, difficulty, exercise_type,
            learner_confidence, source, observed_at
        ) VALUES (?, 'learner', 'networking.transport', 'failure', 1, 6, 'written',
                  95, 'test', ?)
        """,
        (str(uuid.uuid4()), now.isoformat()),
    )
    database.execute(
        """
        UPDATE mastery SET mastery_estimate=0.25, failed_attempts=2,
            last_reviewed=?, next_review=?, review_interval_days=1
        WHERE learner_id='learner' AND concept_id='networking.transport'
        """,
        (now.isoformat(), (now - timedelta(days=1)).isoformat()),
    )
    candidates = AdaptiveScheduler(database).recommend(
        "learner",
        "systems-foundations",
        "generalist",
        LearnerContext(days_until_goal=10),
        now=now,
        limit=15,
    )
    target = next(item for item in candidates if item.concept_id == "networking.transport")
    assert target.factors["confidence"] > 1.8
    assert target.factors["forgetting"] >= 1.2
    assert target.target_difficulty < 4
    assert target.exercise_type != ExerciseType.WRITTEN


def test_active_misconception_changes_priority(initialized: tuple[Database, object]) -> None:
    database, _ = initialized
    now = "2026-02-01T00:00:00+00:00"
    database.execute(
        """
        INSERT INTO misconceptions(
            id, learner_id, concept_id, fingerprint, description, status,
            first_observed, last_observed, frequency, severity
        ) VALUES (?, 'learner', 'performance.measurement', 'fingerprint',
                  'Uses the best sample only', 'active', ?, ?, 3, 5)
        """,
        (str(uuid.uuid4()), now, now),
    )
    candidates = AdaptiveScheduler(database).recommend(
        "learner", "systems-foundations", "generalist", LearnerContext(), limit=15
    )
    target = next(item for item in candidates if item.concept_id == "performance.measurement")
    assert target.factors["misconception"] > 2
