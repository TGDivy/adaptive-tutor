from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.db import Database
from adaptive_tutor.goals import GoalService
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
        "goal",
        "diversity",
        "confidence",
        "prerequisite",
        "urgency",
    }
    assert "unblocks dependent concepts" in recommendations[0].reason


def test_goal_focus_has_bounded_direct_domain_and_prerequisite_relevance(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    GoalService(database).set(
        "learner",
        "systems-foundations",
        "generalist",
        "Build reliable network services.",
        focus_domains=["networking"],
        focus_concepts=["networking.flow-control"],
    )

    candidates = AdaptiveScheduler(database).recommend(
        "learner",
        "systems-foundations",
        "generalist",
        LearnerContext(),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        limit=100,
    )
    by_concept = {candidate.concept_id: candidate for candidate in candidates}

    assert by_concept["networking.flow-control"].factors["goal"] == 1.35
    assert "explicitly prioritized" in by_concept["networking.flow-control"].reason
    assert by_concept["networking.protocol-framing"].factors["goal"] == 1.2
    assert "domain is prioritized" in by_concept["networking.protocol-framing"].reason
    assert by_concept["operating-systems.processes"].factors["goal"] == 1.1
    assert "prerequisite path" in by_concept["operating-systems.processes"].reason
    assert by_concept["performance.measurement"].factors["goal"] == 0.9


def test_persisted_goal_deadline_supplies_urgency_but_context_overrides_it(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    GoalService(database).set(
        "learner",
        "systems-foundations",
        "generalist",
        "Strengthen systems fundamentals.",
        target_date=datetime(2026, 1, 6, tzinfo=UTC).date(),
    )
    scheduler = AdaptiveScheduler(database)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    persisted = scheduler.recommend(
        "learner",
        "systems-foundations",
        "generalist",
        LearnerContext(),
        now=now,
        limit=100,
    )
    overridden = scheduler.recommend(
        "learner",
        "systems-foundations",
        "generalist",
        LearnerContext(days_until_goal=365),
        now=now,
        limit=100,
    )

    assert all(candidate.factors["urgency"] > 1 for candidate in persisted)
    assert all(candidate.factors["urgency"] == 1 for candidate in overridden)


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


def test_recommendations_fit_an_authored_difficulty_range(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    candidates = AdaptiveScheduler(database).recommend(
        "learner", "systems-foundations", "generalist", LearnerContext(), limit=20
    )
    package = CurriculumLoader().load(bundled_curriculum_path())

    for candidate in candidates:
        matching = [
            blueprint
            for blueprint in package.assignments
            if candidate.concept_id in blueprint.concept_ids
            and candidate.exercise_type in blueprint.exercise_types
        ]
        assert matching
        assert any(
            item.difficulty_min <= candidate.target_difficulty <= item.difficulty_max
            for item in matching
        )
