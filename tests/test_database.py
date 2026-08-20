from __future__ import annotations

from pathlib import Path

from adaptive_tutor.db import Database


def test_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    assert database.migrate() == [
        "001",
        "002",
        "003",
        "004",
        "005",
        "006",
        "007",
        "008",
        "009",
    ]
    assert database.migrate() == []
    assert database.migration_versions() == [
        "001",
        "002",
        "003",
        "004",
        "005",
        "006",
        "007",
        "008",
        "009",
    ]
    assert database.integrity_check() == (True, "ok")


def test_online_backup_and_restore(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.sqlite3")
    database.migrate()
    database.execute(
        "INSERT INTO configuration(key, value_json, updated_at) VALUES (?, ?, ?)",
        ("pause", "true", "2026-01-01T00:00:00+00:00"),
    )
    backup = tmp_path / "backups" / "state.sqlite3"
    database.backup(backup)
    database.execute("DELETE FROM configuration WHERE key='pause'")
    database.restore(backup)
    assert database.fetch_one("SELECT value_json FROM configuration WHERE key='pause'") == {
        "value_json": "true"
    }


def test_failed_transaction_rolls_back(database: Database) -> None:
    try:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO configuration(key, value_json, updated_at) VALUES ('x','1','now')"
            )
            raise RuntimeError("abort")
    except RuntimeError:
        pass
    assert database.fetch_one("SELECT * FROM configuration WHERE key='x'") is None
