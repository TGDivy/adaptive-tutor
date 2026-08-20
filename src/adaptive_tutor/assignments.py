"""Assignment generation, validation, persistence, stages, and hints."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Database
from .errors import AssignmentValidationError, ConfigurationError
from .models import (
    AssignmentBundle,
    AssignmentRequest,
    AssignmentStatus,
)
from .time import iso_now
from .trusted_bundles import (
    PublicEvaluatorManifest,
    public_manifest_digest,
    serialize_public_manifest,
)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    checks: dict[str, str]


class AssignmentValidator:
    def validate(
        self,
        bundle: AssignmentBundle,
        request: AssignmentRequest,
        *,
        run_reference: bool = True,
    ) -> ValidationResult:
        checks: dict[str, str] = {}
        if set(bundle.concepts) != set(request.target_concepts):
            raise AssignmentValidationError(
                "Generated assignment concept scope does not match the request"
            )
        checks["concept_coverage"] = "target concepts are represented"
        if bundle.exercise_type not in request.context.allowed_formats:
            raise AssignmentValidationError("Generated assignment uses a disallowed format")
        checks["format"] = "format is allowed"
        if bundle.expected_minutes > request.context.available_minutes + 15:
            raise AssignmentValidationError("Expected duration exceeds available study time")
        checks["duration"] = "duration is plausible"
        recent = request.recent_assignments[-3:]
        if any(
            item.get("slug") == bundle.slug
            and (
                item.get("primary_concept") is None
                or item.get("primary_concept") in bundle.concepts
            )
            for item in recent
        ):
            raise AssignmentValidationError("Assignment repeats a recent problem")
        same_format = sum(
            item.get("exercise_type") == bundle.exercise_type.value for item in recent[-2:]
        )
        if same_format == 2:
            raise AssignmentValidationError("Assignment repeats the last two exercise formats")
        checks["diversity"] = "recent topic and format repetition avoided"
        instructions = "\n".join(
            item.content for item in bundle.files if item.role == "instructions"
        ).lower()
        if len(instructions) < 250:
            raise AssignmentValidationError("Instructions are not sufficient to solve the task")
        for phrase in ("target concept", "difficulty"):
            if phrase not in instructions:
                raise AssignmentValidationError(f"Instructions omit {phrase}")
        if "[[" in instructions or "]]" in instructions:
            raise AssignmentValidationError("Instructions contain unresolved template tokens")
        constraints = bundle.hidden_evaluator.get("constraints", [])
        if not isinstance(constraints, list) or len(constraints) < 3:
            raise AssignmentValidationError("Hidden evaluator has insufficient constraints")
        hints = bundle.hidden_evaluator.get("hints", [])
        if not isinstance(hints, list) or len(hints) != 5:
            raise AssignmentValidationError("Assignment must provide five progressive hints")
        if len(bundle.reference_expectations) < 2:
            raise AssignmentValidationError("Trusted reference expectations are incomplete")
        if len(set(bundle.tags)) < 3:
            raise AssignmentValidationError("Assignment needs descriptive concept and format tags")
        roles = {item.role for item in bundle.files}
        if bundle.validation_command and not {
            "starter",
            "public_test",
            "reference",
            "evaluator",
        }.issubset(roles):
            raise AssignmentValidationError(
                "Executable assignments need starter, tests, reference, and hidden evaluator"
            )
        checks["consistency"] = "instructions and evaluator constraints are populated"
        if bundle.difficulty != request.target_difficulty:
            raise AssignmentValidationError("Generated difficulty does not match the request")
        checks["difficulty"] = "difficulty matches scheduler target"
        if run_reference and bundle.validation_command:
            self._run_reference(bundle)
            checks["reference"] = "trusted reference passes public and hidden tests"
        else:
            checks["reference"] = "reference expectations validated structurally"
        return ValidationResult(valid=True, checks=checks)

    def _run_reference(self, bundle: AssignmentBundle) -> None:
        if bundle.validation_command[:3] != ["python", "-m", "pytest"] or any(
            argument not in {"-q"} for argument in bundle.validation_command[3:]
        ):
            raise AssignmentValidationError(
                "Reference validation must use the fixed Python pytest harness"
            )
        with tempfile.TemporaryDirectory(prefix="adaptive-tutor-reference-") as temporary:
            root = Path(temporary)
            by_path = {item.path: item for item in bundle.files}
            for item in bundle.files:
                if item.role in {"reference", "evaluator"}:
                    continue
                target = root / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item.content, encoding="utf-8")
            replacements = bundle.hidden_evaluator.get("reference_replacements", {})
            if not isinstance(replacements, dict):
                raise AssignmentValidationError("reference_replacements must be a mapping")
            for target_name, source_name in replacements.items():
                source = by_path.get(str(source_name))
                if source is None or source.role != "reference":
                    raise AssignmentValidationError(f"Missing trusted reference: {source_name}")
                target = _safe_workspace_path(root, str(target_name))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.content, encoding="utf-8")
            extras = bundle.hidden_evaluator.get("extra_tests", {})
            if not isinstance(extras, dict):
                raise AssignmentValidationError("extra_tests must be a mapping")
            for target_name, source_name in extras.items():
                source = by_path.get(str(source_name))
                if source is None or source.role != "evaluator":
                    raise AssignmentValidationError(f"Missing trusted evaluator: {source_name}")
                target = _safe_workspace_path(root, str(target_name))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.content, encoding="utf-8")
            command = [sys.executable, *bundle.validation_command[1:]]
            safe_env = {
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONPATH": str(root),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            # The command is an argv array, the executable is resolved from a
            # narrow tool name, no shell is used, and the environment is scrubbed.
            result = subprocess.run(  # noqa: S603
                command,
                cwd=root,
                env=safe_env,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            if result.returncode != 0:
                output = (result.stdout + "\n" + result.stderr)[-4000:]
                raise AssignmentValidationError(f"Trusted reference failed its harness:\n{output}")


def _safe_workspace_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if root.resolve() not in candidate.parents:
        raise AssignmentValidationError(f"Evaluator path escapes the workspace: {relative}")
    return candidate


class AssignmentService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        request: AssignmentRequest,
        bundle: AssignmentBundle,
        validation: ValidationResult,
    ) -> dict[str, Any]:
        if not validation.valid:
            raise AssignmentValidationError("Cannot persist an invalid assignment")
        now = iso_now()
        assignment_uuid = str(uuid.uuid4())
        with self.database.transaction() as connection:
            active = connection.execute(
                """
                SELECT id, title FROM assignments
                WHERE learner_id=? AND status IN
                    ('validated', 'published', 'submitted', 'reviewing', 'follow_up')
                """,
                (request.learner_id,),
            ).fetchone()
            if active:
                raise ConfigurationError(
                    f"Assignment '{active['title']}' is already active ({active['id']})"
                )
            counter_row = connection.execute(
                "SELECT value_json FROM configuration WHERE key='assignment_counter'"
            ).fetchone()
            counter = int(json.loads(counter_row[0])) + 1 if counter_row else 1
            connection.execute(
                """
                INSERT INTO configuration(key, value_json, updated_at) VALUES
                    ('assignment_counter', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (json.dumps(counter), now),
            )
            assignment_id = f"A-{counter:04d}"
            branch = f"assignment/{counter:04d}-{bundle.slug}"
            connection.execute(
                """
                INSERT INTO assignments(
                    id, learner_id, curriculum_id, profile_id, slug, title,
                    exercise_type, difficulty, expected_minutes, status, branch_name,
                    bundle_json, current_stage, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'validated', ?, ?, 1, ?, ?)
                """,
                (
                    assignment_id,
                    request.learner_id,
                    request.curriculum_id,
                    request.profile_id,
                    bundle.slug,
                    bundle.title,
                    bundle.exercise_type.value,
                    bundle.difficulty,
                    bundle.expected_minutes,
                    branch,
                    bundle.model_dump_json(),
                    now,
                    now,
                ),
            )
            for index, concept_id in enumerate(bundle.concepts):
                connection.execute(
                    """
                    INSERT INTO assignment_concepts(assignment_id, concept_id, is_primary)
                    VALUES (?, ?, ?)
                    """,
                    (assignment_id, concept_id, int(index == 0)),
                )
            for stage in bundle.stages:
                connection.execute(
                    """
                    INSERT INTO assignment_stages(
                        assignment_id, stage_number, title, instructions, unlock_condition,
                        unlocked_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assignment_id,
                        stage.number,
                        stage.title,
                        stage.instructions,
                        stage.unlock_condition,
                        now if stage.number == 1 else None,
                    ),
                )
            connection.execute(
                """
                INSERT INTO activity(id, learner_id, kind, summary, metadata_json, occurred_at)
                VALUES (?, ?, 'assignment_created', ?, ?, ?)
                """,
                (
                    assignment_uuid,
                    request.learner_id,
                    f"Created {assignment_id}: {bundle.title}",
                    json.dumps({"assignment_id": assignment_id, "checks": validation.checks}),
                    now,
                ),
            )
        return {
            "id": assignment_id,
            "title": bundle.title,
            "branch_name": branch,
            "status": AssignmentStatus.VALIDATED.value,
            "bundle": bundle,
        }

    def active(self, learner_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            """
            SELECT * FROM assignments WHERE learner_id=? AND status IN
                ('validated', 'published', 'submitted', 'reviewing', 'follow_up')
            ORDER BY created_at DESC LIMIT 1
            """,
            (learner_id,),
        )
        if row:
            row["bundle"] = AssignmentBundle.model_validate_json(row.pop("bundle_json"))
        return row

    def public_files(
        self,
        assignment_id: str,
        *,
        evaluator_manifest: PublicEvaluatorManifest | None = None,
    ) -> dict[str, str]:
        row = self.database.fetch_one(
            "SELECT bundle_json, branch_name, current_stage FROM assignments WHERE id=?",
            (assignment_id,),
        )
        if row is None:
            raise ConfigurationError(f"Unknown assignment: {assignment_id}")
        bundle = AssignmentBundle.model_validate_json(row["bundle_json"])
        public = {
            item.path: item.content
            for item in bundle.files
            if item.role not in {"reference", "evaluator"}
        }
        metadata = {
            "schema_version": "1.0",
            "id": assignment_id,
            "slug": bundle.slug,
            "concepts": bundle.concepts,
            "exercise_type": bundle.exercise_type.value,
            "difficulty": bundle.difficulty,
            "expected_minutes": bundle.expected_minutes,
            "current_stage": int(row["current_stage"]),
            "tags": bundle.tags,
            "selection_reason": bundle.selection_reason,
        }
        if evaluator_manifest is not None:
            if (
                evaluator_manifest.assignment_id != assignment_id
                or evaluator_manifest.branch != str(row["branch_name"])
            ):
                raise ConfigurationError("Public evaluator manifest does not match the assignment")
            metadata["branch"] = str(row["branch_name"])
            metadata["evaluator_manifest_digest"] = public_manifest_digest(evaluator_manifest)
            metadata["evaluator_key_id"] = evaluator_manifest.key_id
            metadata["evaluator_kit_digest"] = evaluator_manifest.evaluator_kit_digest
            public[".adaptive-tutor/evaluator-manifest.json"] = serialize_public_manifest(
                evaluator_manifest
            )
        public[".adaptive-tutor/assignment.json"] = (
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )
        return public

    def next_hint(self, assignment_id: str, learner_id: str) -> tuple[int, str]:
        row = self.database.fetch_one(
            "SELECT bundle_json FROM assignments WHERE id=? AND learner_id=?",
            (assignment_id, learner_id),
        )
        if row is None:
            raise ConfigurationError(f"No assignment {assignment_id} for learner")
        bundle = AssignmentBundle.model_validate_json(row["bundle_json"])
        hints = bundle.hidden_evaluator.get("hints", [])
        if not isinstance(hints, list) or len(hints) != 5:
            raise ConfigurationError("Assignment does not provide five progressive hints")
        existing = self.database.fetch_all(
            "SELECT level FROM hints WHERE assignment_id=? AND learner_id=? ORDER BY level",
            (assignment_id, learner_id),
        )
        level = min(len(existing) + 1, 5)
        content = str(hints[level - 1])
        self.database.execute(
            """
            INSERT OR IGNORE INTO hints(id, assignment_id, learner_id, level, content, requested_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), assignment_id, learner_id, level, content, iso_now()),
        )
        return level, content

    def unlock_follow_up(self, assignment_id: str) -> int | None:
        now = iso_now()
        with self.database.transaction() as connection:
            assignment = connection.execute(
                "SELECT current_stage FROM assignments WHERE id=?", (assignment_id,)
            ).fetchone()
            if assignment is None:
                raise ConfigurationError(f"Unknown assignment: {assignment_id}")
            current = int(assignment["current_stage"])
            following = connection.execute(
                """
                SELECT stage_number FROM assignment_stages
                WHERE assignment_id=? AND stage_number=?
                """,
                (assignment_id, current + 1),
            ).fetchone()
            if following is None:
                return None
            connection.execute(
                """
                UPDATE assignment_stages SET completed_at=?
                WHERE assignment_id=? AND stage_number=?
                """,
                (now, assignment_id, current),
            )
            connection.execute(
                """
                UPDATE assignment_stages SET unlocked_at=?
                WHERE assignment_id=? AND stage_number=?
                """,
                (now, assignment_id, current + 1),
            )
            connection.execute(
                """
                UPDATE assignments SET current_stage=?, status='follow_up', updated_at=?
                WHERE id=?
                """,
                (current + 1, now, assignment_id),
            )
            return current + 1
