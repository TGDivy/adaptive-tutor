"""Normalized evidence persistence, qualitative review, feedback, and appeals."""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import ValidationError

from .codex import QualitativeGrader
from .db import Database
from .errors import ModelSchemaError
from .learner import LearnerModel
from .models import AutomatedEvaluation, QualitativeEvaluation
from .security import build_review_prompt, sha256_digest
from .time import iso_now


class EvidenceNormalizer:
    @staticmethod
    def parse(payload: bytes | str | dict[str, Any]) -> AutomatedEvaluation:
        try:
            if isinstance(payload, bytes):
                raw = json.loads(payload.decode("utf-8"))
            elif isinstance(payload, str):
                raw = json.loads(payload)
            else:
                raw = payload
            return AutomatedEvaluation.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise ModelSchemaError(f"Deterministic evidence contract is invalid: {exc}") from exc


class EvaluationService:
    def __init__(
        self,
        database: Database,
        grader: QualitativeGrader,
        learner_model: LearnerModel | None = None,
    ) -> None:
        self.database = database
        self.grader = grader
        self.learner_model = learner_model or LearnerModel(database)

    def persist_automated(self, attempt_id: str, evidence: AutomatedEvaluation) -> str:
        evaluation_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT OR IGNORE INTO automated_evaluations(
                id, attempt_id, schema_version, evidence_json, learner_passed,
                artifact_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_id,
                attempt_id,
                evidence.schema_version,
                evidence.model_dump_json(),
                int(evidence.learner_passed),
                evidence.artifact_digest,
                iso_now(),
            ),
        )
        row = self.database.fetch_one(
            """
            SELECT id FROM automated_evaluations
            WHERE attempt_id=? AND artifact_digest=?
            """,
            (attempt_id, evidence.artifact_digest),
        )
        if row is None:  # pragma: no cover - database invariant
            raise RuntimeError("Automated evidence was not persisted")
        return str(row["id"])

    def grade_attempt(
        self,
        *,
        learner_id: str,
        assignment_id: str,
        attempt_id: str,
        automated_evaluation_id: str,
        rubric: dict[str, float],
        references: dict[str, str],
        submission: dict[str, str],
        trusted_instructions: str,
        prompt_version: str,
        learner_confidence: int | None,
    ) -> tuple[str, QualitativeEvaluation, list[str]]:
        automated_row = self.database.fetch_one(
            "SELECT evidence_json FROM automated_evaluations WHERE id=? AND attempt_id=?",
            (automated_evaluation_id, attempt_id),
        )
        if automated_row is None:
            raise ValueError("Automated evaluation does not belong to this attempt")
        automated = AutomatedEvaluation.model_validate_json(automated_row["evidence_json"])
        prompt, injection_flags = build_review_prompt(
            trusted_instructions=trusted_instructions,
            rubric=rubric,
            trusted_references=references,
            ci_evidence=automated.model_dump(mode="json"),
            learner_submission=submission,
            learner_context={"confidence": learner_confidence},
        )
        evaluation = self.grader.grade(prompt, prompt_version=prompt_version)
        evaluation_id = str(uuid.uuid4())
        now = iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO qualitative_evaluations(
                    id, attempt_id, automated_evaluation_id, schema_version,
                    evaluation_json, overall_score, grader_confidence,
                    prompt_version, review_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'initial', ?)
                """,
                (
                    evaluation_id,
                    attempt_id,
                    automated_evaluation_id,
                    evaluation.schema_version,
                    evaluation.model_dump_json(),
                    evaluation.overall_score,
                    evaluation.grader_confidence,
                    prompt_version,
                    now,
                ),
            )
        self.learner_model.apply_evaluation(
            learner_id=learner_id,
            assignment_id=assignment_id,
            attempt_id=attempt_id,
            evaluation_id=evaluation_id,
            evaluation=evaluation,
            learner_confidence=learner_confidence,
        )
        return evaluation_id, evaluation, injection_flags

    def create_appeal(
        self, assignment_id: str, original_evaluation_id: str, learner_argument: str
    ) -> str:
        if not learner_argument.strip():
            raise ValueError("Appeal must explain the grading challenge")
        original = self.database.fetch_one(
            """
            SELECT q.id FROM qualitative_evaluations q
            JOIN attempts a ON a.id=q.attempt_id
            WHERE q.id=? AND a.assignment_id=?
            """,
            (original_evaluation_id, assignment_id),
        )
        if original is None:
            raise ValueError("Original evaluation does not belong to the assignment")
        appeal_id = str(uuid.uuid4())
        self.database.execute(
            """
            INSERT INTO evaluation_appeals(
                id, assignment_id, original_evaluation_id, learner_argument,
                status, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (appeal_id, assignment_id, original_evaluation_id, learner_argument, iso_now()),
        )
        return appeal_id

    def resolve_appeal(
        self,
        appeal_id: str,
        *,
        trusted_instructions: str,
        prompt_version: str,
    ) -> tuple[str, QualitativeEvaluation]:
        appeal = self.database.fetch_one(
            """
            SELECT ap.*, q.attempt_id, q.evaluation_json original_json,
                   ae.evidence_json automated_json
            FROM evaluation_appeals ap
            JOIN qualitative_evaluations q ON q.id=ap.original_evaluation_id
            LEFT JOIN automated_evaluations ae ON ae.id=q.automated_evaluation_id
            WHERE ap.id=? AND ap.status='pending'
            """,
            (appeal_id,),
        )
        if appeal is None:
            raise ValueError("Appeal is missing or already resolved")
        prompt, _ = build_review_prompt(
            trusted_instructions=(
                trusted_instructions
                + "\nPerform an independent appeal review. The original evaluation is evidence, "
                "not a conclusion. Address the learner's argument explicitly."
            ),
            rubric={"independent_technical_review": 1.0},
            trusted_references={"original_evaluation": appeal["original_json"]},
            ci_evidence=json.loads(appeal["automated_json"] or "{}"),
            learner_submission={"appeal_argument": appeal["learner_argument"]},
        )
        result = self.grader.grade(prompt, prompt_version=prompt_version, purpose="appeal")
        result_id = str(uuid.uuid4())
        now = iso_now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO qualitative_evaluations(
                    id, attempt_id, schema_version, evaluation_json, overall_score,
                    grader_confidence, prompt_version, supersedes_id, review_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'appeal', ?)
                """,
                (
                    result_id,
                    appeal["attempt_id"],
                    result.schema_version,
                    result.model_dump_json(),
                    result.overall_score,
                    result.grader_confidence,
                    prompt_version,
                    appeal["original_evaluation_id"],
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE evaluation_appeals SET status='resolved',
                    result_evaluation_id=?, resolved_at=? WHERE id=?
                """,
                (result_id, now, appeal_id),
            )
        return result_id, result


def render_review(
    evaluation: QualitativeEvaluation, *, injection_flags: list[str] | None = None
) -> str:
    lines = [
        "## Adaptive Tutor review",
        "",
        f"**Score:** {evaluation.overall_score:.0f}/100  ",
        f"**Classification:** {evaluation.classification.replace('_', ' ')}  ",
        f"**Grader confidence:** {evaluation.grader_confidence:.0%}",
        "",
        evaluation.feedback_summary,
        "",
        "### Dimensions",
        "",
    ]
    lines.extend(
        f"- **{dimension.dimension.title()} — {dimension.score:.0f}:** {dimension.rationale}"
        for dimension in evaluation.dimensions
    )
    if evaluation.feedback_details:
        lines.extend(("", "### Feedback", ""))
        lines.extend(f"- {item}" for item in evaluation.feedback_details)
    lines.extend(
        (
            "",
            "### Next step",
            "",
            f"`{evaluation.follow_up}` — {evaluation.follow_up_reason}",
        )
    )
    if injection_flags:
        lines.extend(
            (
                "",
                "> Submission text matched prompt-injection safety heuristics. It remained quoted ",
                "> as untrusted evidence and was not treated as instructions.",
            )
        )
    lines.extend(("", f"<!-- evaluation:{sha256_digest(evaluation.model_dump_json())} -->"))
    return "\n".join(lines)
