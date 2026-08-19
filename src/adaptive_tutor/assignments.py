"""Assignment generation, validation, persistence, stages, and hints."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Database
from .errors import AssignmentValidationError, ConfigurationError
from .models import (
    AssignmentBundle,
    AssignmentFile,
    AssignmentRequest,
    AssignmentStage,
    AssignmentStatus,
    ExerciseType,
)
from .time import iso_now


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    checks: dict[str, str]


class TemplateAssignmentGenerator:
    """Deterministic safe generator used for demo and model-independent fallback."""

    def generate(self, request: AssignmentRequest) -> AssignmentBundle:
        primary = request.target_concepts[0]
        exercise_type = self._select_format(request)
        if exercise_type in {
            ExerciseType.IMPLEMENTATION,
            ExerciseType.DEBUGGING,
            ExerciseType.REFACTORING,
            ExerciseType.PERFORMANCE,
        }:
            return self._coding_bundle(request, primary, exercise_type)
        return self._reasoning_bundle(request, primary, exercise_type)

    @staticmethod
    def _select_format(request: AssignmentRequest) -> ExerciseType:
        recent = [item.get("exercise_type") for item in request.recent_assignments[-3:]]
        for allowed in request.context.allowed_formats:
            if allowed.value not in recent:
                return allowed
        return request.context.allowed_formats[0]

    def _coding_bundle(
        self,
        request: AssignmentRequest,
        primary: str,
        exercise_type: ExerciseType,
    ) -> AssignmentBundle:
        minutes = min(request.context.available_minutes, 55)
        title = "Repair a bounded work queue"
        slug = "bounded-work-queue"
        readme = f"""# {title}

The queue in `src/bounded_queue.py` confuses capacity with current occupancy
after a sequence of removals and insertions. Repair the implementation while
preserving these observable constraints:

1. insertion returns `False` only while the queue contains exactly `capacity` items;
2. removal returns `None` only while the queue contains no items;
3. values leave in insertion order, including across storage wraparound;
4. construction rejects non-positive capacity;
5. no operation changes the configured capacity.

Add at least one focused regression test. Run `python -m pytest -q`.

In `ANSWER.md`, state the invariant that distinguishes empty from full, explain
why the original representation loses it, and report confidence from 0-100.

Target concept: `{primary}`. Expected time: {minutes} minutes. Difficulty:
{request.target_difficulty}/10.
"""
        starter = """class BoundedQueue:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._items = [None] * capacity
        self._read = 0
        self._write = 0

    @property
    def capacity(self) -> int:
        return len(self._items)

    def put(self, value: object) -> bool:
        if self._write == self._read and self._items[self._write] is not None:
            return False
        self._items[self._write] = value
        self._write = (self._write + 1) % self.capacity
        return True

    def get(self) -> object | None:
        if self._read == self._write:
            return None
        value = self._items[self._read]
        self._items[self._read] = None
        self._read = (self._read + 1) % self.capacity
        return value
"""
        public_test = """import pytest

from src.bounded_queue import BoundedQueue


def test_fifo_and_capacity() -> None:
    queue = BoundedQueue(2)
    assert queue.put("a")
    assert queue.put("b")
    assert not queue.put("c")
    assert queue.get() == "a"
    assert queue.get() == "b"
    assert queue.get() is None


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BoundedQueue(0)
"""
        reference = """class BoundedQueue:
    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._items = [None] * capacity
        self._read = 0
        self._write = 0
        self._size = 0

    @property
    def capacity(self) -> int:
        return len(self._items)

    def put(self, value: object) -> bool:
        if self._size == self.capacity:
            return False
        self._items[self._write] = value
        self._write = (self._write + 1) % self.capacity
        self._size += 1
        return True

    def get(self) -> object | None:
        if self._size == 0:
            return None
        value = self._items[self._read]
        self._items[self._read] = None
        self._read = (self._read + 1) % self.capacity
        self._size -= 1
        return value
"""
        hidden_test = """from src.bounded_queue import BoundedQueue


def test_wraparound_and_repeated_cycles() -> None:
    queue = BoundedQueue(3)
    expected = []
    for cycle in range(30):
        for offset in range(3):
            value = (cycle, offset)
            assert queue.put(value)
            expected.append(value)
        assert not queue.put("overflow")
        for _ in range(3):
            assert queue.get() == expected.pop(0)
        assert queue.get() is None
"""
        return AssignmentBundle(
            slug=slug,
            title=title,
            summary="Repair an ambiguous ring-buffer representation and explain its invariant.",
            concepts=request.target_concepts,
            exercise_type=exercise_type,
            difficulty=request.target_difficulty,
            expected_minutes=minutes,
            files=[
                AssignmentFile(path="README.md", content=readme, role="instructions"),
                AssignmentFile(path="src/__init__.py", content="", role="starter"),
                AssignmentFile(path="src/bounded_queue.py", content=starter, role="starter"),
                AssignmentFile(path="tests/test_queue.py", content=public_test, role="public_test"),
                AssignmentFile(
                    path="reference/bounded_queue.py", content=reference, role="reference"
                ),
                AssignmentFile(
                    path="evaluator/test_hidden.py", content=hidden_test, role="evaluator"
                ),
                AssignmentFile(
                    path="ANSWER.md",
                    content="# Analysis\n\nInvariant:\n\nCause:\n\nConfidence (0-100):\n",
                    role="starter",
                ),
            ],
            hidden_evaluator={
                "reference_replacements": {
                    "src/bounded_queue.py": "reference/bounded_queue.py"
                },
                "extra_tests": {"tests/test_hidden.py": "evaluator/test_hidden.py"},
                "constraints": [
                    "insertion fails exactly at configured capacity",
                    "removal preserves insertion order",
                    "wraparound remains correct",
                    "non-positive capacity is rejected",
                ],
                "hints": [
                    "Write down all states in which the two indices are equal.",
                    "The missing concept is an explicit occupancy invariant.",
                    "Track information that changes on every successful put and get.",
                    "Add a size field bounded by zero and capacity, then use it for empty/full.",
                    "Increment size after put, decrement after get, and compare it to 0/capacity.",
                ],
            },
            rubric={
                "correctness": 0.45,
                "tests": 0.20,
                "reasoning": 0.25,
                "communication": 0.10,
            },
            reference_expectations=[
                "The representation retains an independent occupancy bit or count.",
                "Tests cover fill, drain, wraparound, and repeated reuse.",
                "The explanation states the empty/full invariant explicitly.",
            ],
            stages=[
                AssignmentStage(
                    number=1,
                    title="Correctness repair",
                    instructions="Repair the queue and state its invariant.",
                    unlock_condition="Deterministic correctness checks pass.",
                ),
                AssignmentStage(
                    number=2,
                    title="Representation follow-up",
                    instructions=(
                        "Replace the count with another unambiguous representation and compare "
                        "its API and storage trade-offs."
                    ),
                    unlock_condition="Stage 1 passes with a supported explanation.",
                ),
            ],
            validation_command=["python", "-m", "pytest", "-q"],
        )

    @staticmethod
    def _reasoning_bundle(
        request: AssignmentRequest,
        primary: str,
        exercise_type: ExerciseType,
    ) -> AssignmentBundle:
        title = "Backpressure incident review"
        instructions = f"""# {title}

A service reads framed messages from clients, places decoded requests in an
unbounded in-memory queue, and processes them with four workers. During a load
spike, resident memory grows until the service is terminated. Median latency
looks healthy while tail latency and timeouts rise.

Produce `RESPONSE.md` with:

1. a causal explanation that separates transport, queueing, and scheduling;
2. three measurements that would confirm or falsify the explanation;
3. a bounded design with explicit overload behavior;
4. the strongest trade-off or failure mode introduced by that design;
5. confidence from 0-100.

Target concept: `{primary}`. Format: `{exercise_type.value}`. Difficulty:
{request.target_difficulty}/10. Keep the response below 900 words.
"""
        return AssignmentBundle(
            slug="backpressure-incident-review",
            title=title,
            summary="Diagnose a queueing failure and design explicit overload behavior.",
            concepts=request.target_concepts,
            exercise_type=exercise_type,
            difficulty=request.target_difficulty,
            expected_minutes=min(request.context.available_minutes, 60),
            files=[
                AssignmentFile(path="README.md", content=instructions, role="instructions"),
                AssignmentFile(
                    path="RESPONSE.md",
                    content=(
                        "# Diagnosis\n\n# Evidence\n\n# Design\n\n# Trade-off\n\n"
                        "Confidence (0-100):\n"
                    ),
                    role="starter",
                ),
                AssignmentFile(
                    path="reference/expectations.md",
                    content=(
                        "A strong response bounds admission or queue length, propagates "
                        "backpressure, and measures distributions and queue residence time."
                    ),
                    role="reference",
                ),
            ],
            hidden_evaluator={
                "constraints": [
                    "causal explanation spans all three named layers",
                    "measurements are falsifiable",
                    "overload behavior is explicit",
                    "confidence is present",
                ],
                "hints": [
                    "Draw the path from socket receive buffer to completed response.",
                    "Apply Little's Law to the in-memory queue.",
                    "Measure queue depth and residence time as distributions.",
                    "Choose where admission stops and what the client observes.",
                    "A complete answer bounds work, propagates pressure, and names fairness costs.",
                ],
            },
            rubric={
                "reasoning": 0.35,
                "evidence": 0.25,
                "design": 0.25,
                "communication": 0.15,
            },
            reference_expectations=[
                "Queue growth is connected to arrival and service rates.",
                "Measurements include queue residence time and tail distributions.",
                "The design specifies bounded admission and client-visible overload behavior.",
            ],
            stages=[
                AssignmentStage(
                    number=1,
                    title="Incident diagnosis",
                    instructions="Diagnose the observed failure and propose a bounded design.",
                    unlock_condition="The causal chain and evidence are technically supported.",
                ),
                AssignmentStage(
                    number=2,
                    title="Adversarial follow-up",
                    instructions="Revisit the design when one tenant produces half the load.",
                    unlock_condition="Stage 1 is accepted.",
                ),
            ],
            validation_command=[],
        )


class AssignmentValidator:
    def validate(
        self,
        bundle: AssignmentBundle,
        request: AssignmentRequest,
        *,
        run_reference: bool = True,
    ) -> ValidationResult:
        checks: dict[str, str] = {}
        if not set(bundle.concepts).issubset(set(request.target_concepts)):
            raise AssignmentValidationError("Generated assignment targets unrequested concepts")
        checks["concept_coverage"] = "target concepts are represented"
        if bundle.exercise_type not in request.context.allowed_formats:
            raise AssignmentValidationError("Generated assignment uses a disallowed format")
        checks["format"] = "format is allowed"
        if bundle.expected_minutes > request.context.available_minutes + 15:
            raise AssignmentValidationError("Expected duration exceeds available study time")
        checks["duration"] = "duration is plausible"
        recent = request.recent_assignments[-3:]
        if any(item.get("slug") == bundle.slug for item in recent):
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
        for phrase in ("target concept", "difficulty"):
            if phrase not in instructions:
                raise AssignmentValidationError(f"Instructions omit {phrase}")
        constraints = bundle.hidden_evaluator.get("constraints", [])
        if not isinstance(constraints, list) or len(constraints) < 2:
            raise AssignmentValidationError("Hidden evaluator has insufficient constraints")
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
                target = root / str(target_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.content, encoding="utf-8")
            extras = bundle.hidden_evaluator.get("extra_tests", {})
            if not isinstance(extras, dict):
                raise AssignmentValidationError("extra_tests must be a mapping")
            for target_name, source_name in extras.items():
                source = by_path.get(str(source_name))
                if source is None or source.role != "evaluator":
                    raise AssignmentValidationError(f"Missing trusted evaluator: {source_name}")
                target = root / str(target_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.content, encoding="utf-8")
            executable = shutil.which(bundle.validation_command[0])
            if executable is None:
                raise AssignmentValidationError(
                    f"Reference validation tool is unavailable: {bundle.validation_command[0]}"
                )
            command = [executable, *bundle.validation_command[1:]]
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

    def public_files(self, assignment_id: str) -> dict[str, str]:
        row = self.database.fetch_one(
            "SELECT bundle_json FROM assignments WHERE id=?", (assignment_id,)
        )
        if row is None:
            raise ConfigurationError(f"Unknown assignment: {assignment_id}")
        bundle = AssignmentBundle.model_validate_json(row["bundle_json"])
        return {
            item.path: item.content
            for item in bundle.files
            if item.role not in {"reference", "evaluator"}
        }

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
