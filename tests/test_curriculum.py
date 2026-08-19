from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from adaptive_tutor.curriculum import (
    CurriculumLoader,
    bundled_curriculum_path,
    curriculum_digest,
)
from adaptive_tutor.db import Database
from adaptive_tutor.errors import CurriculumError


def test_bundled_curriculum_is_complete_and_persisted(
    initialized: tuple[Database, object],
) -> None:
    database, _ = initialized
    concepts = database.fetch_all("SELECT * FROM concepts ORDER BY id")
    profiles = database.fetch_all("SELECT * FROM profiles ORDER BY id")
    relationships = database.fetch_all("SELECT * FROM concept_relationships")
    assert len(concepts) == 15
    assert {item["domain"] for item in concepts} == {
        "programming",
        "operating-systems",
        "concurrency",
        "networking",
        "performance",
        "reasoning",
    }
    assert {item["id"] for item in profiles} == {"generalist", "service-engineer"}
    assert len(relationships) >= 12
    assert curriculum_digest(bundled_curriculum_path()).startswith("sha256:")


def test_curriculum_rejects_prerequisite_cycles(tmp_path: Path) -> None:
    target = tmp_path / "cycle"
    shutil.copytree(bundled_curriculum_path(), target)
    path = target / "prerequisites.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["prerequisites"]["programming.invariants"] = ["programming.data-structures"]
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(CurriculumError, match="cycle"):
        CurriculumLoader().load(target)


def test_curriculum_rejects_reference_path_escape(tmp_path: Path) -> None:
    target = tmp_path / "escape"
    shutil.copytree(bundled_curriculum_path(), target)
    path = target / "concepts.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["concepts"][0]["reference_files"] = ["../outside.md"]
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    with pytest.raises(CurriculumError, match="Invalid reference path"):
        CurriculumLoader().load(target)
