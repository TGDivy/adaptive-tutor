from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from adaptive_tutor.curriculum import CurriculumLoader, bundled_curriculum_path
from adaptive_tutor.db import Database
from adaptive_tutor.models import CurriculumPackage


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "state.sqlite3")
    instance.migrate()
    return instance


@pytest.fixture
def curriculum(database: Database) -> CurriculumPackage:
    package = CurriculumLoader().load(bundled_curriculum_path())
    CurriculumLoader().persist(package, database, "learner")
    return package


@pytest.fixture
def initialized(
    database: Database, curriculum: CurriculumPackage
) -> Iterator[tuple[Database, CurriculumPackage]]:
    yield database, curriculum
