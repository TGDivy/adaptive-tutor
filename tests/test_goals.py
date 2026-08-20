from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from adaptive_tutor.db import Database
from adaptive_tutor.goals import GoalService, GoalStatus


def test_goal_set_active_history_and_idempotence(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    service = GoalService(database)
    created = service.set(
        "learner",
        "systems-foundations",
        "generalist",
        "  Build reliable network services.  ",
        target_date=date(2026, 6, 30),
        focus_domains=["networking", "networking"],
        focus_concepts=["networking.flow-control", "networking.flow-control"],
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert created.revision == 1
    assert created.statement == "Build reliable network services."
    assert created.focus_domains == ["networking"]
    assert created.focus_concepts == ["networking.flow-control"]
    assert created.status == GoalStatus.ACTIVE
    assert service.active("learner", "systems-foundations") == created

    repeated = service.set(
        "learner",
        "systems-foundations",
        "generalist",
        "Build reliable network services.",
        target_date=date(2026, 6, 30),
        focus_domains=[" networking "],
        focus_concepts=[" networking.flow-control "],
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert repeated == created
    assert service.history("learner", "systems-foundations") == [created]


def test_goal_revisions_are_ordered_and_scoped(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    service = GoalService(database)
    first = service.set(
        "learner",
        "systems-foundations",
        "generalist",
        "Strengthen systems fundamentals.",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = service.set(
        "learner",
        "systems-foundations",
        "generalist",
        "Design reliable network services.",
        focus_domains=["networking"],
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    other = service.set(
        "other-learner",
        "systems-foundations",
        "generalist",
        "Learn concurrency fundamentals.",
        now=datetime(2026, 1, 3, tzinfo=UTC),
    )

    history = service.history("learner", "systems-foundations")
    assert [goal.id for goal in history] == [second.id, first.id]
    assert [goal.revision for goal in history] == [2, 1]
    assert history[0].status == GoalStatus.ACTIVE
    assert history[1].status == GoalStatus.SUPERSEDED
    assert history[1].superseded_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert service.active("other-learner", "systems-foundations") == other
    assert service.history("learner", "systems-foundations", limit=1) == [second]


def test_goal_supersession_rolls_back_when_insert_fails(
    initialized: tuple[Database, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _ = initialized
    service = GoalService(database)
    existing = service.set(
        "learner",
        "systems-foundations",
        "generalist",
        "Learn transport fundamentals.",
    )
    monkeypatch.setattr("adaptive_tutor.goals.uuid.uuid4", lambda: existing.id)

    with pytest.raises(sqlite3.IntegrityError):
        service.set(
            "learner",
            "systems-foundations",
            "generalist",
            "Learn flow control.",
        )

    assert service.active("learner", "systems-foundations") == existing
    assert service.history("learner", "systems-foundations") == [existing]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"profile_id": "missing"}, "Unknown curriculum profile"),
        ({"focus_domains": ["missing"]}, "Unknown curriculum domain"),
        ({"focus_concepts": ["missing.concept"]}, "Unknown curriculum concept"),
    ],
)
def test_goal_rejects_unknown_curriculum_selectors_without_mutation(
    initialized: tuple[Database, object], overrides: dict[str, object], message: str
) -> None:
    database, _ = initialized
    service = GoalService(database)
    existing = service.set(
        "learner",
        "systems-foundations",
        "generalist",
        "Build systems fundamentals.",
    )
    arguments: dict[str, object] = {
        "learner_id": "learner",
        "curriculum_id": "systems-foundations",
        "profile_id": "generalist",
        "statement": "Change the active goal.",
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=message):
        service.set(**arguments)  # type: ignore[arg-type]

    assert service.active("learner", "systems-foundations") == existing
    assert service.history("learner", "systems-foundations") == [existing]
