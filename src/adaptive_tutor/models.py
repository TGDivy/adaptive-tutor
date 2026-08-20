"""Validated contracts shared by generators, evaluators, storage, and APIs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExerciseType(StrEnum):
    IMPLEMENTATION = "implementation"
    DEBUGGING = "debugging"
    CODE_REVIEW = "code_review"
    PERFORMANCE = "performance_investigation"
    WRITTEN = "written"
    MATHEMATICS = "mathematics"
    QUIZ = "quiz"
    SYSTEM_DESIGN = "system_design"
    EXPLAIN_CODE = "explain_the_code"
    REFACTORING = "refactoring"
    FOLLOW_UP = "interviewer_follow_up"


class MisconceptionStatus(StrEnum):
    SUSPECTED = "suspected"
    ACTIVE = "active"
    CHALLENGED = "challenged"
    RESOLVED = "resolved"
    RECURRED = "recurred"


class AssignmentStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    SUBMITTED = "submitted"
    REVIEWING = "reviewing"
    FOLLOW_UP = "follow_up"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CurriculumMetadata(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str = Field(min_length=2, max_length=120)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=10, max_length=1000)
    default_profile: str
    generation_guidance: str = Field(min_length=10)
    grading_guidance: str = Field(min_length=10)


class ConceptDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,100}$")
    name: str = Field(min_length=2, max_length=120)
    domain: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=10)
    importance: float = Field(ge=0.1, le=2.0)
    base_difficulty: int = Field(ge=1, le=10)
    exercise_types: list[ExerciseType] = Field(min_length=2)
    prerequisites: list[str] = Field(default_factory=list)
    reference_files: list[str] = Field(default_factory=list)
    generation_guidance: str = ""
    grading_guidance: str = ""


class ProfileDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str
    description: str
    domain_weights: dict[str, float] = Field(default_factory=dict)
    concept_weights: dict[str, float] = Field(default_factory=dict)

    @field_validator("domain_weights", "concept_weights")
    @classmethod
    def positive_weights(cls, value: dict[str, float]) -> dict[str, float]:
        if any(weight <= 0 or weight > 3 for weight in value.values()):
            raise ValueError("profile weights must be in (0, 3]")
        return value


class LearnerContext(StrictModel):
    available_minutes: int = Field(default=45, ge=5, le=480)
    energy: Literal["low", "medium", "high"] = "medium"
    days_until_goal: int | None = Field(default=None, ge=0, le=3650)
    allowed_formats: list[ExerciseType] = Field(default_factory=lambda: list(ExerciseType))


class SchedulerCandidate(StrictModel):
    concept_id: str
    exercise_type: ExerciseType
    target_difficulty: int = Field(ge=1, le=10)
    priority: float = Field(ge=0)
    factors: dict[str, float]
    reason: str


class AssignmentRequest(StrictModel):
    learner_id: str
    curriculum_id: str
    profile_id: str
    target_concepts: list[str] = Field(min_length=1, max_length=5)
    active_misconceptions: list[dict[str, Any]] = Field(default_factory=list)
    recent_assignments: list[dict[str, Any]] = Field(default_factory=list)
    target_difficulty: int = Field(ge=1, le=10)
    context: LearnerContext
    trusted_references: dict[str, str] = Field(default_factory=dict)
    concept_state: dict[str, dict[str, float | int | str | None]] = Field(
        default_factory=dict
    )
    selection_reason: str = ""
    scheduler_factors: dict[str, float] = Field(default_factory=dict)


class AssignmentFile(StrictModel):
    path: str = Field(pattern=r"^[A-Za-z0-9_.\-/]+$")
    content: str
    role: Literal["instructions", "starter", "public_test", "reference", "evaluator"]

    @field_validator("path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or not value or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("assignment file paths must be normalized relative paths")
        return value


class AssignmentStage(StrictModel):
    number: int = Field(ge=1, le=20)
    title: str
    instructions: str
    unlock_condition: str


class AssignmentBundle(StrictModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    title: str = Field(min_length=5, max_length=160)
    summary: str = Field(min_length=20)
    concepts: list[str] = Field(min_length=1, max_length=5)
    exercise_type: ExerciseType
    difficulty: int = Field(ge=1, le=10)
    expected_minutes: int = Field(ge=5, le=480)
    learner_confidence_requested: bool = True
    files: list[AssignmentFile] = Field(min_length=2)
    hidden_evaluator: dict[str, Any]
    rubric: dict[str, float]
    reference_expectations: list[str] = Field(min_length=1)
    stages: list[AssignmentStage] = Field(default_factory=list)
    validation_command: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    selection_reason: str = ""
    generator_metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def weights_and_files_are_coherent(self) -> AssignmentBundle:
        if abs(sum(self.rubric.values()) - 1.0) > 0.001:
            raise ValueError("rubric weights must sum to 1.0")
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("assignment paths must be unique")
        if not any(item.role == "instructions" for item in self.files):
            raise ValueError("assignment must include instructions")
        if self.stages and [stage.number for stage in self.stages] != list(
            range(1, len(self.stages) + 1)
        ):
            raise ValueError("assignment stage numbers must be consecutive")
        return self


class AssignmentBlueprint(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    title: str = Field(min_length=5, max_length=160)
    summary: str = Field(min_length=20)
    concept_ids: list[str] = Field(min_length=1)
    exercise_types: list[ExerciseType] = Field(min_length=1)
    difficulty_min: int = Field(ge=1, le=10)
    difficulty_max: int = Field(ge=1, le=10)
    expected_minutes: int = Field(ge=5, le=480)
    files: list[AssignmentFile] = Field(min_length=2)
    hidden_evaluator: dict[str, Any]
    rubric: dict[str, float]
    reference_expectations: list[str] = Field(min_length=1)
    stages: list[AssignmentStage] = Field(default_factory=list)
    validation_command: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def blueprint_is_coherent(self) -> AssignmentBlueprint:
        if self.difficulty_min > self.difficulty_max:
            raise ValueError("blueprint difficulty_min cannot exceed difficulty_max")
        AssignmentBundle(
            slug=self.slug,
            title=self.title,
            summary=self.summary,
            concepts=[self.concept_ids[0]],
            exercise_type=self.exercise_types[0],
            difficulty=self.difficulty_min,
            expected_minutes=self.expected_minutes,
            files=self.files,
            hidden_evaluator=self.hidden_evaluator,
            rubric=self.rubric,
            reference_expectations=self.reference_expectations,
            stages=self.stages,
            validation_command=self.validation_command,
            tags=self.tags,
        )
        return self


class CurriculumPackage(StrictModel):
    root: Path
    metadata: CurriculumMetadata
    concepts: list[ConceptDefinition]
    profiles: list[ProfileDefinition]
    assignments: list[AssignmentBlueprint]
    prompts: dict[str, str]
    fixtures: dict[str, Any]


class AutomatedCheck(StrictModel):
    name: str
    status: Literal["pass", "fail", "error", "skipped"]
    category: Literal[
        "compile",
        "test",
        "integration",
        "stress",
        "sanitizer",
        "static_analysis",
        "benchmark",
        "golden_output",
        "allocation",
        "policy",
    ]
    summary: str
    duration_ms: int = Field(default=0, ge=0)
    metrics: dict[str, float | int | str] = Field(default_factory=dict)


class AutomatedEvaluation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    checks: list[AutomatedCheck]
    started_at: datetime
    completed_at: datetime
    runner: str
    evaluator_binding: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evaluator_key_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    dispatch_nonce: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    manifest_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    workflow_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    workflow_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40,64}$")
    evaluator_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    evaluator_kit_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    repository_id: int | None = Field(default=None, ge=1)
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @property
    def learner_passed(self) -> bool:
        relevant = [check for check in self.checks if check.status != "skipped"]
        return bool(relevant) and all(check.status == "pass" for check in relevant)

    @property
    def has_operational_error(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    def computed_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"artifact_digest"})
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def with_computed_digest(self) -> AutomatedEvaluation:
        return self.model_copy(update={"artifact_digest": self.computed_digest()})


class DimensionScore(StrictModel):
    dimension: Literal[
        "correctness",
        "reasoning",
        "tradeoffs",
        "design",
        "communication",
        "performance",
    ]
    score: float = Field(ge=0, le=100)
    rationale: str = Field(min_length=3)


class ConceptEvidence(StrictModel):
    concept_id: str
    outcome: Literal["success", "partial", "failure", "not_observed"]
    strength: float = Field(ge=0, le=1)
    difficulty: int = Field(ge=1, le=10)
    exercise_type: ExerciseType
    rationale: str
    transfer_context: str | None = None


class MisconceptionFinding(StrictModel):
    concept_id: str
    description: str = Field(min_length=8)
    evidence: str = Field(min_length=3)
    severity: int = Field(ge=1, le=5)
    action: Literal["suspect", "confirm", "challenge", "resolve", "recur"]


class QualitativeEvaluation(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    overall_score: float = Field(ge=0, le=100)
    dimensions: list[DimensionScore] = Field(min_length=3)
    grader_confidence: float = Field(ge=0, le=1)
    concept_evidence: list[ConceptEvidence]
    misconceptions: list[MisconceptionFinding]
    feedback_summary: str = Field(min_length=10)
    feedback_details: list[str]
    classification: Literal[
        "wrong",
        "incomplete",
        "correct_weak_justification",
        "valid_alternative",
        "style_preference",
        "performance_tradeoff",
        "correct",
    ]
    follow_up: Literal["none", "new_stage", "new_assignment", "human_review"]
    follow_up_reason: str
    escalation_recommended: bool

    @field_validator("dimensions")
    @classmethod
    def unique_dimensions(cls, value: list[DimensionScore]) -> list[DimensionScore]:
        names = [item.dimension for item in value]
        if len(names) != len(set(names)):
            raise ValueError("dimension scores must be unique")
        return value


class ReadinessDomain(StrictModel):
    domain: str
    readiness: float | None = Field(default=None, ge=0, le=1)
    uncertainty: float | None = Field(default=None, ge=0, le=1)
    concept_count: int = Field(ge=0)
    assessed_concept_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)


class RuntimeStatus(StrictModel):
    paused: bool
    active_curriculum: str
    active_assignment: dict[str, Any] | None
    readiness: list[ReadinessDomain]
    weaknesses: list[dict[str, Any]]
    misconceptions: list[dict[str, Any]]
    upcoming_reviews: list[dict[str, Any]]
    recent_scores: list[dict[str, Any]]
    recent_changes: list[dict[str, Any]]
    recent_activity: list[dict[str, Any]]
    confidence_calibration: dict[str, float | int]
    model_usage: dict[str, float | int]
