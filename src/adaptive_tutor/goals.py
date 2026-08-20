"""Durable, revisioned learning goals and explicit curriculum focus."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, field_validator, model_validator

from .db import Database
from .models import StrictModel
from .time import utc_now


class GoalStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class LearningGoal(StrictModel):
    id: str
    learner_id: str
    curriculum_id: str
    profile_id: str
    revision: int = Field(ge=1)
    statement: str = Field(min_length=1, max_length=2000)
    target_date: date | None = None
    focus_domains: list[str] = Field(default_factory=list)
    focus_concepts: list[str] = Field(default_factory=list)
    status: GoalStatus
    created_at: datetime
    superseded_at: datetime | None = None

    @field_validator("statement")
    @classmethod
    def normalize_statement(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("learning goal statement cannot be empty")
        return normalized

    @field_validator("focus_domains", "focus_concepts")
    @classmethod
    def normalize_selectors(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip() for item in value})
        if "" in normalized:
            raise ValueError("learning goal focus selectors cannot be empty")
        return normalized

    @model_validator(mode="after")
    def status_matches_supersession(self) -> LearningGoal:
        if self.status == GoalStatus.ACTIVE and self.superseded_at is not None:
            raise ValueError("active learning goals cannot have a superseded timestamp")
        if self.status == GoalStatus.SUPERSEDED and self.superseded_at is None:
            raise ValueError("superseded learning goals require a timestamp")
        return self


class GoalService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def active(self, learner_id: str, curriculum_id: str) -> LearningGoal | None:
        row = self.database.fetch_one(
            """
            SELECT * FROM learning_goals
            WHERE learner_id=? AND curriculum_id=? AND status='active'
            """,
            (learner_id, curriculum_id),
        )
        return _goal_from_row(row) if row is not None else None

    def set(
        self,
        learner_id: str,
        curriculum_id: str,
        profile_id: str,
        statement: str,
        *,
        target_date: date | None = None,
        focus_domains: list[str] | None = None,
        focus_concepts: list[str] | None = None,
        now: datetime | None = None,
    ) -> LearningGoal:
        if not learner_id.strip():
            raise ValueError("learner_id cannot be empty")
        statement_value = statement.strip()
        if not statement_value:
            raise ValueError("learning goal statement cannot be empty")
        if len(statement_value) > 2000:
            raise ValueError("learning goal statement cannot exceed 2000 characters")
        domains = _normalize_selectors(focus_domains or [], "domain")
        concepts = _normalize_selectors(focus_concepts or [], "concept")
        created_at = _iso_timestamp(now)
        target_date_value = target_date.isoformat() if target_date is not None else None

        with self.database.transaction() as connection:
            curriculum = connection.execute(
                "SELECT id FROM curricula WHERE id=?", (curriculum_id,)
            ).fetchone()
            if curriculum is None:
                raise ValueError(f"Unknown curriculum: {curriculum_id}")
            profile = connection.execute(
                "SELECT id FROM profiles WHERE curriculum_id=? AND id=?",
                (curriculum_id, profile_id),
            ).fetchone()
            if profile is None:
                raise ValueError(f"Unknown curriculum profile: {curriculum_id}/{profile_id}")
            concept_rows = connection.execute(
                """
                SELECT id, domain, name, goal_terms_json
                FROM concepts WHERE curriculum_id=?
                """,
                (curriculum_id,),
            ).fetchall()
            valid_concepts = {str(row["id"]) for row in concept_rows}
            valid_domains = {str(row["domain"]) for row in concept_rows}
            _validate_selectors(domains, valid_domains, "domain")
            _validate_selectors(concepts, valid_concepts, "concept")
            if not domains and not concepts:
                concepts = _infer_goal_concepts(statement_value, concept_rows)
                if not concepts:
                    raise ValueError(
                        f"Learning goal does not match active curriculum '{curriculum_id}'. "
                        "Load a compatible curriculum or specify a curriculum domain or concept."
                    )

            active_row = connection.execute(
                """
                SELECT * FROM learning_goals
                WHERE learner_id=? AND curriculum_id=? AND status='active'
                """,
                (learner_id, curriculum_id),
            ).fetchone()
            if active_row is not None and _same_goal(
                active_row,
                profile_id=profile_id,
                statement=statement_value,
                target_date=target_date_value,
                focus_domains=domains,
                focus_concepts=concepts,
            ):
                return _goal_from_row(dict(active_row))

            revision_row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision
                FROM learning_goals WHERE learner_id=? AND curriculum_id=?
                """,
                (learner_id, curriculum_id),
            ).fetchone()
            revision = int(revision_row["next_revision"])
            if active_row is not None:
                connection.execute(
                    """
                    UPDATE learning_goals
                    SET status='superseded', superseded_at=?
                    WHERE id=?
                    """,
                    (created_at, active_row["id"]),
                )

            goal_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO learning_goals(
                    id, learner_id, curriculum_id, profile_id, revision, statement,
                    target_date, focus_domains_json, focus_concepts_json, status,
                    created_at, superseded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL)
                """,
                (
                    goal_id,
                    learner_id,
                    curriculum_id,
                    profile_id,
                    revision,
                    statement_value,
                    target_date_value,
                    json.dumps(domains),
                    json.dumps(concepts),
                    created_at,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM learning_goals WHERE id=?", (goal_id,)
            ).fetchone()
            if stored is None:  # pragma: no cover - protected by the transaction
                raise RuntimeError("Learning goal insert did not persist")
            return _goal_from_row(dict(stored))

    def history(
        self, learner_id: str, curriculum_id: str, *, limit: int = 20
    ) -> list[LearningGoal]:
        if not 1 <= limit <= 1000:
            raise ValueError("learning goal history limit must be between 1 and 1000")
        rows = self.database.fetch_all(
            """
            SELECT * FROM learning_goals
            WHERE learner_id=? AND curriculum_id=?
            ORDER BY revision DESC
            LIMIT ?
            """,
            (learner_id, curriculum_id, limit),
        )
        return [_goal_from_row(row) for row in rows]


def _goal_from_row(row: dict[str, Any]) -> LearningGoal:
    return LearningGoal.model_validate(
        {
            "id": row["id"],
            "learner_id": row["learner_id"],
            "curriculum_id": row["curriculum_id"],
            "profile_id": row["profile_id"],
            "revision": row["revision"],
            "statement": row["statement"],
            "target_date": row["target_date"],
            "focus_domains": json.loads(str(row["focus_domains_json"])),
            "focus_concepts": json.loads(str(row["focus_concepts_json"])),
            "status": row["status"],
            "created_at": row["created_at"],
            "superseded_at": row["superseded_at"],
        }
    )


def _normalize_selectors(values: list[str], kind: str) -> list[str]:
    normalized = sorted({value.strip() for value in values})
    if "" in normalized:
        raise ValueError(f"learning goal {kind} selectors cannot be empty")
    return normalized


def _validate_selectors(values: list[str], valid: set[str], kind: str) -> None:
    unknown = sorted(set(values) - valid)
    if unknown:
        raise ValueError(f"Unknown curriculum {kind}: {', '.join(unknown)}")


def _infer_goal_concepts(statement: str, rows: list[Any]) -> list[str]:
    normalized_statement = _normalize_goal_text(statement)
    padded_statement = f" {normalized_statement} "
    matches: list[tuple[int, str]] = []
    for row in rows:
        candidates = {
            str(row["name"]),
            str(row["domain"]).replace("-", " "),
            *json.loads(str(row["goal_terms_json"])),
        }
        matched_lengths = [
            len(normalized.split())
            for item in candidates
            if (normalized := _normalize_goal_text(str(item)))
            and f" {normalized} " in padded_statement
        ]
        if matched_lengths:
            matches.append((max(matched_lengths), str(row["id"])))
    if not matches:
        return []
    strongest = max(length for length, _ in matches)
    threshold = max(1, strongest - 1)
    return sorted(concept_id for length, concept_id in matches if length >= threshold)


def _normalize_goal_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _same_goal(
    row: Any,
    *,
    profile_id: str,
    statement: str,
    target_date: str | None,
    focus_domains: list[str],
    focus_concepts: list[str],
) -> bool:
    return bool(
        row["profile_id"] == profile_id
        and row["statement"] == statement
        and row["target_date"] == target_date
        and json.loads(str(row["focus_domains_json"])) == focus_domains
        and json.loads(str(row["focus_concepts_json"])) == focus_concepts
    )


def _iso_timestamp(value: datetime | None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="seconds")
