"""Transactional SQLite storage and migration runner."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import ConfigurationError


class Database:
    """Small explicit database layer; SQLite is the canonical product state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def migrate(self) -> list[str]:
        applied: list[str] = []
        with self.transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) STRICT
                """
            )
            known = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            migration_root = resources.files("adaptive_tutor.migrations")
            migrations = sorted(
                (item for item in migration_root.iterdir() if item.name.endswith(".sql")),
                key=lambda item: item.name,
            )
            for migration in migrations:
                version = migration.name.split("_", 1)[0]
                if version in known:
                    continue
                sql = migration.read_text(encoding="utf-8")
                for statement in _split_sql(sql):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )
                applied.append(version)
        return applied

    def migration_versions(self) -> list[str]:
        try:
            with contextlib.closing(self.connect()) as connection:
                return [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
        except sqlite3.OperationalError:
            return []

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        with self.transaction() as connection:
            cursor = connection.execute(sql, parameters)
            return cursor.rowcount

    def fetch_one(self, sql: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row is not None else None

    def fetch_all(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with contextlib.closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def integrity_check(self) -> tuple[bool, str]:
        try:
            with contextlib.closing(self.connect()) as connection:
                result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if result != "ok" or foreign_keys:
                return False, f"integrity={result}; foreign_key_errors={len(foreign_keys)}"
            return True, "ok"
        except sqlite3.Error as exc:
            return False, str(exc)

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self.connect()) as source, contextlib.closing(
            sqlite3.connect(destination)
        ) as target:
            source.backup(target)
        destination.chmod(0o600)

    def restore(self, source: Path) -> None:
        if not source.is_file():
            raise ConfigurationError(f"Backup does not exist: {source}")
        with contextlib.closing(sqlite3.connect(source)) as backup, contextlib.closing(
            self.connect()
        ) as target:
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ConfigurationError("Backup failed SQLite integrity check")
            backup.backup(target)


def _split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise ConfigurationError("Incomplete SQL migration statement")
    return statements
