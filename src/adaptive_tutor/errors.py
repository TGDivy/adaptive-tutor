"""Typed product errors used across service boundaries."""

from __future__ import annotations

from enum import StrEnum


class FailureKind(StrEnum):
    LEARNER = "learner_failure"
    INFRASTRUCTURE = "infrastructure_failure"
    GENERATOR = "generator_failure"
    INVALID_ASSIGNMENT = "invalid_assignment"
    MODEL = "model_failure"
    SCHEMA = "schema_failure"
    SECURITY = "security_failure"
    EXTERNAL_DEPENDENCY = "external_dependency_failure"


class TutorError(RuntimeError):
    """Base error with a stable machine-readable category."""

    kind = FailureKind.INFRASTRUCTURE

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ConfigurationError(TutorError):
    pass


class SecurityError(TutorError):
    kind = FailureKind.SECURITY


class CurriculumError(TutorError):
    kind = FailureKind.INVALID_ASSIGNMENT


class AssignmentValidationError(TutorError):
    kind = FailureKind.INVALID_ASSIGNMENT


class ModelError(TutorError):
    kind = FailureKind.MODEL


class ModelSchemaError(TutorError):
    kind = FailureKind.SCHEMA


class ExternalServiceError(TutorError):
    kind = FailureKind.EXTERNAL_DEPENDENCY
