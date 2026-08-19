"""Curriculum-owned assignment selection and rendering."""

from __future__ import annotations

from typing import Any

from .errors import AssignmentValidationError
from .models import (
    AssignmentBlueprint,
    AssignmentBundle,
    AssignmentFile,
    AssignmentRequest,
    AssignmentStage,
    CurriculumPackage,
)


class CurriculumAssignmentGenerator:
    """Select an authored problem family without embedding subject matter in core."""

    def __init__(self, package: CurriculumPackage) -> None:
        self.package = package
        self._concepts = {item.id: item for item in package.concepts}

    def generate(self, request: AssignmentRequest) -> AssignmentBundle:
        primary = request.target_concepts[0]
        if primary not in self._concepts:
            raise AssignmentValidationError(f"Unknown target concept: {primary}")
        allowed = set(request.context.allowed_formats)
        eligible = [
            item
            for item in self.package.assignments
            if primary in item.concept_ids and allowed.intersection(item.exercise_types)
        ]
        if not eligible:
            requested = ", ".join(sorted(item.value for item in allowed))
            raise AssignmentValidationError(
                f"Curriculum has no authored assignment for {primary} in: {requested}"
            )
        recent_pairs = [
            (str(item.get("blueprint_id") or item.get("slug")), item.get("primary_concept"))
            for item in request.recent_assignments[-5:]
        ]
        fresh = [
            item
            for item in eligible
            if not any(
                identity in {item.id, item.slug}
                and (recent_concept is None or recent_concept == primary)
                for identity, recent_concept in recent_pairs
            )
        ]
        candidates = fresh or eligible
        blueprint = min(
            candidates,
            key=lambda item: (
                not (item.difficulty_min <= request.target_difficulty <= item.difficulty_max),
                abs(_difficulty_midpoint(item) - request.target_difficulty),
                item.expected_minutes > request.context.available_minutes + 15,
                item.expected_minutes,
                item.id,
            ),
        )
        exercise_type = next(
            item for item in request.context.allowed_formats if item in blueprint.exercise_types
        )
        duration = min(blueprint.expected_minutes, request.context.available_minutes + 15)
        tokens = self._tokens(request, blueprint, duration)
        return AssignmentBundle(
            slug=blueprint.slug,
            title=_render(blueprint.title, tokens),
            summary=_render(blueprint.summary, tokens),
            concepts=list(request.target_concepts),
            exercise_type=exercise_type,
            difficulty=request.target_difficulty,
            expected_minutes=duration,
            files=[
                AssignmentFile(
                    path=item.path,
                    role=item.role,
                    content=_render(item.content, tokens),
                )
                for item in blueprint.files
            ],
            hidden_evaluator=_render_value(blueprint.hidden_evaluator, tokens),
            rubric=dict(blueprint.rubric),
            reference_expectations=[
                _render(item, tokens) for item in blueprint.reference_expectations
            ],
            stages=[
                AssignmentStage(
                    number=item.number,
                    title=_render(item.title, tokens),
                    instructions=_render(item.instructions, tokens),
                    unlock_condition=_render(item.unlock_condition, tokens),
                )
                for item in blueprint.stages
            ],
            validation_command=list(blueprint.validation_command),
            tags=sorted({*blueprint.tags, primary, exercise_type.value}),
            selection_reason=request.selection_reason,
            generator_metadata={
                "blueprint_id": blueprint.id,
                "curriculum_id": self.package.metadata.id,
                "curriculum_version": self.package.metadata.version,
                "reference_count": str(len(request.trusted_references)),
                "misconception_count": str(len(request.active_misconceptions)),
            },
        )

    def _tokens(
        self,
        request: AssignmentRequest,
        blueprint: AssignmentBlueprint,
        duration: int,
    ) -> dict[str, str]:
        concept = self._concepts[request.target_concepts[0]]
        state = request.concept_state.get(concept.id, {})
        evidence_count = int(state.get("evidence_count") or 0)
        if evidence_count:
            mastery = float(state.get("mastery_estimate") or 0)
            state_summary = f"{mastery:.0%} mastery from {evidence_count} evidence points"
        else:
            state_summary = "not yet assessed"
        difficulty_scope = (
            "Focus on the stated behavior and one decisive boundary case."
            if request.target_difficulty <= 3
            else "Cover interacting boundary cases and justify the chosen trade-off."
            if request.target_difficulty <= 7
            else "Handle adversarial scale and failure conditions; quantify the trade-off."
        )
        return {
            "concept_id": concept.id,
            "concept_name": concept.name,
            "concept_domain": concept.domain.replace("-", " "),
            "difficulty": str(request.target_difficulty),
            "expected_minutes": str(duration),
            "exercise_type": next(
                item.value
                for item in request.context.allowed_formats
                if item in blueprint.exercise_types
            ).replace("_", " "),
            "selection_reason": request.selection_reason or "This is the next best evidence gap.",
            "state_summary": state_summary,
            "difficulty_scope": difficulty_scope,
        }


def _difficulty_midpoint(blueprint: AssignmentBlueprint) -> float:
    return (blueprint.difficulty_min + blueprint.difficulty_max) / 2


def _render(value: str, tokens: dict[str, str]) -> str:
    rendered = value
    for name, replacement in tokens.items():
        rendered = rendered.replace(f"[[{name}]]", replacement)
    return rendered


def _render_value(value: Any, tokens: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _render(value, tokens)
    if isinstance(value, list):
        return [_render_value(item, tokens) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, tokens) for key, item in value.items()}
    return value
