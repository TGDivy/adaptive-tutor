from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from adaptive_tutor.config import TutorSettings
from adaptive_tutor.db import Database
from adaptive_tutor.doctor import Doctor


def test_offline_doctor_reports_actionable_local_state(
    initialized: tuple[Database, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _ = initialized
    settings = TutorSettings(
        data_dir=tmp_path / "private-state",
        database_path=database.path,
        learner_id="learner",
        codex={"enabled": False},
    )
    settings.ensure_runtime_dirs()

    def unavailable(*_: object, **__: object) -> None:
        raise httpx.ConnectError("service is stopped")

    monkeypatch.setattr("adaptive_tutor.doctor.httpx.get", unavailable)
    checks = {item.name: item for item in Doctor(settings, database).run(online=False)}

    assert checks["Database"].status == "pass"
    assert checks["Configuration"].status == "pass"
    assert checks["Filesystem permissions"].status == "pass"
    assert checks["Codex CLI"].status == "warn"
    assert checks["GitHub App configuration"].status == "warn"
    assert checks["GitHub connectivity"].status == "warn"
    assert checks["Service health"].status == "warn"
    assert all(item.detail for item in checks.values())


def test_doctor_rejects_world_readable_private_state(
    database: Database, tmp_path: Path
) -> None:
    data_dir = tmp_path / "private-state"
    settings = TutorSettings(data_dir=data_dir, database_path=database.path)
    settings.ensure_runtime_dirs()
    data_dir.chmod(0o755)

    check = Doctor(settings, database)._filesystem()
    assert check.status == "fail"
    assert str(data_dir) in check.detail
    assert check.fix
