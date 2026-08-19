"""Private-data-friendly curriculum package loading and validation."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .db import Database
from .errors import CurriculumError
from .models import (
    ConceptDefinition,
    CurriculumMetadata,
    CurriculumPackage,
    ProfileDefinition,
)
from .time import iso_now

REQUIRED_FILES = ("curriculum.yaml", "concepts.yaml", "prerequisites.yaml", "profiles.yaml")
REQUIRED_PROMPTS = ("generation", "grading")


def bundled_curriculum_path() -> Path:
    editable = Path(__file__).resolve().parents[2] / "curricula" / "systems-foundations"
    if editable.is_dir():
        return editable
    packaged = resources.files("adaptive_tutor").joinpath(
        "bundled_curricula", "systems-foundations"
    )
    return Path(str(packaged))


class CurriculumLoader:
    def load(self, root: Path) -> CurriculumPackage:
        root = root.expanduser().resolve()
        if not root.is_dir():
            raise CurriculumError(f"Curriculum directory does not exist: {root}")
        for required in REQUIRED_FILES:
            if not (root / required).is_file():
                raise CurriculumError(f"Curriculum is missing {required}")
        try:
            metadata = CurriculumMetadata.model_validate(self._yaml(root / "curriculum.yaml"))
            raw_concepts = self._list_yaml(root / "concepts.yaml", "concepts")
            prerequisite_data = self._yaml(root / "prerequisites.yaml")
            prerequisites = prerequisite_data.get("prerequisites", prerequisite_data)
            concepts = []
            for raw in raw_concepts:
                item = dict(raw)
                item["prerequisites"] = list(prerequisites.get(item.get("id"), []))
                concepts.append(ConceptDefinition.model_validate(item))
            profiles = [
                ProfileDefinition.model_validate(item)
                for item in self._list_yaml(root / "profiles.yaml", "profiles")
            ]
        except (ValidationError, TypeError, ValueError) as exc:
            raise CurriculumError(f"Curriculum schema validation failed: {exc}") from exc

        prompts = self._load_prompts(root)
        fixtures = self._load_fixtures(root)
        self._validate_graph(metadata, concepts, profiles, root)
        return CurriculumPackage(
            root=root,
            metadata=metadata,
            concepts=concepts,
            profiles=profiles,
            prompts=prompts,
            fixtures=fixtures,
        )

    def persist(self, package: CurriculumPackage, database: Database, learner_id: str) -> None:
        now = iso_now()
        digest = curriculum_digest(package.root)
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO curricula(
                    id, name, version, description, source_path, content_digest, loaded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    version=excluded.version,
                    description=excluded.description,
                    source_path=excluded.source_path,
                    content_digest=excluded.content_digest,
                    loaded_at=excluded.loaded_at
                """,
                (
                    package.metadata.id,
                    package.metadata.name,
                    package.metadata.version,
                    package.metadata.description,
                    str(package.root),
                    digest,
                    now,
                ),
            )
            concept_ids = {concept.id for concept in package.concepts}
            placeholders = ",".join("?" for _ in concept_ids)
            if concept_ids:
                connection.execute(
                    f"DELETE FROM concepts WHERE curriculum_id=? AND id NOT IN ({placeholders})",  # noqa: S608
                    (package.metadata.id, *sorted(concept_ids)),
                )
            for concept in package.concepts:
                connection.execute(
                    """
                    INSERT INTO concepts(
                        id, curriculum_id, name, domain, description, importance,
                        base_difficulty, exercise_types_json, generation_guidance,
                        grading_guidance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        curriculum_id=excluded.curriculum_id,
                        name=excluded.name,
                        domain=excluded.domain,
                        description=excluded.description,
                        importance=excluded.importance,
                        base_difficulty=excluded.base_difficulty,
                        exercise_types_json=excluded.exercise_types_json,
                        generation_guidance=excluded.generation_guidance,
                        grading_guidance=excluded.grading_guidance
                    """,
                    (
                        concept.id,
                        package.metadata.id,
                        concept.name,
                        concept.domain,
                        concept.description,
                        concept.importance,
                        concept.base_difficulty,
                        json.dumps([item.value for item in concept.exercise_types]),
                        concept.generation_guidance,
                        concept.grading_guidance,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO mastery(learner_id, concept_id, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    (learner_id, concept.id, now),
                )
            connection.execute(
                "DELETE FROM concept_relationships WHERE curriculum_id=?",
                (package.metadata.id,),
            )
            for concept in package.concepts:
                for prerequisite in concept.prerequisites:
                    connection.execute(
                        """
                        INSERT INTO concept_relationships(
                            curriculum_id, concept_id, prerequisite_id, relationship_type, weight
                        ) VALUES (?, ?, ?, 'prerequisite', 1.0)
                        """,
                        (package.metadata.id, concept.id, prerequisite),
                    )
            connection.execute("DELETE FROM profiles WHERE curriculum_id=?", (package.metadata.id,))
            for profile in package.profiles:
                connection.execute(
                    """
                    INSERT INTO profiles(
                        curriculum_id, id, name, description,
                        domain_weights_json, concept_weights_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        package.metadata.id,
                        profile.id,
                        profile.name,
                        profile.description,
                        json.dumps(profile.domain_weights, sort_keys=True),
                        json.dumps(profile.concept_weights, sort_keys=True),
                    ),
                )
            for purpose, template in package.prompts.items():
                template_digest = hashlib.sha256(template.encode()).hexdigest()
                connection.execute(
                    """
                    INSERT INTO prompt_versions(
                        id, purpose, version, template_digest, template_text, active, created_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(purpose, version) DO UPDATE SET
                        template_digest=excluded.template_digest,
                        template_text=excluded.template_text,
                        active=1
                    """,
                    (
                        f"{package.metadata.id}:{purpose}:{package.metadata.version}",
                        purpose,
                        package.metadata.version,
                        template_digest,
                        template,
                        now,
                    ),
                )

    @staticmethod
    def _yaml(path: Path) -> dict[str, Any]:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise CurriculumError(f"Cannot read {path.name}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise CurriculumError(f"{path.name} must contain a mapping")
        return loaded

    def _list_yaml(self, path: Path, key: str) -> list[dict[str, Any]]:
        loaded = self._yaml(path)
        value = loaded.get(key)
        if not isinstance(value, list) or not value:
            raise CurriculumError(f"{path.name} must contain a non-empty '{key}' list")
        if any(not isinstance(item, dict) for item in value):
            raise CurriculumError(f"Every {key} entry must be a mapping")
        return value

    @staticmethod
    def _load_prompts(root: Path) -> dict[str, str]:
        prompt_root = root / "prompts"
        prompts: dict[str, str] = {}
        for name in REQUIRED_PROMPTS:
            path = prompt_root / f"{name}.md"
            if not path.is_file():
                raise CurriculumError(f"Curriculum is missing prompts/{name}.md")
            content = path.read_text(encoding="utf-8").strip()
            if len(content) < 20:
                raise CurriculumError(f"Prompt {name} is too short")
            prompts[name] = content
        return prompts

    @staticmethod
    def _load_fixtures(root: Path) -> dict[str, Any]:
        fixture_root = root / "fixtures"
        fixtures: dict[str, Any] = {}
        if not fixture_root.exists():
            return fixtures
        for path in sorted(fixture_root.glob("*.json")):
            try:
                fixtures[path.stem] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CurriculumError(f"Invalid fixture {path.name}: {exc}") from exc
        return fixtures

    @staticmethod
    def _validate_graph(
        metadata: CurriculumMetadata,
        concepts: list[ConceptDefinition],
        profiles: list[ProfileDefinition],
        root: Path,
    ) -> None:
        ids = [concept.id for concept in concepts]
        if len(ids) != len(set(ids)):
            raise CurriculumError("Concept identifiers must be unique")
        known = set(ids)
        for concept in concepts:
            unknown = set(concept.prerequisites) - known
            if unknown:
                raise CurriculumError(f"{concept.id} has unknown prerequisites: {sorted(unknown)}")
            if concept.id in concept.prerequisites:
                raise CurriculumError(f"{concept.id} cannot require itself")
            for relative in concept.reference_files:
                candidate = (root / "references" / relative).resolve()
                reference_root = (root / "references").resolve()
                if reference_root not in candidate.parents or not candidate.is_file():
                    raise CurriculumError(f"Invalid reference path for {concept.id}: {relative}")
        _assert_acyclic(concepts)
        profile_ids = {profile.id for profile in profiles}
        if metadata.default_profile not in profile_ids:
            raise CurriculumError("default_profile does not exist")
        domains = {concept.domain for concept in concepts}
        for profile in profiles:
            if set(profile.domain_weights) - domains:
                raise CurriculumError(f"Profile {profile.id} references an unknown domain")
            if set(profile.concept_weights) - known:
                raise CurriculumError(f"Profile {profile.id} references an unknown concept")


def _assert_acyclic(concepts: list[ConceptDefinition]) -> None:
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {concept.id: 0 for concept in concepts}
    for concept in concepts:
        for prerequisite in concept.prerequisites:
            outgoing[prerequisite].append(concept.id)
            indegree[concept.id] += 1
    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for dependent in outgoing[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if visited != len(concepts):
        raise CurriculumError("Prerequisite graph contains a cycle")


def curriculum_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()
