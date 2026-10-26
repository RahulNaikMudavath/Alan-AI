"""Versioned schema migrations.

Migrations are never applied as a side effect of application startup. Startup
calls :func:`require_current` (through :class:`app.db.Database`) and refuses to
run when the database is missing, behind, or newer than this build.

Version state lives in SQLite's ``PRAGMA user_version``. Each file is applied
exactly once, in numeric order, inside one ``BEGIN IMMEDIATE`` transaction that
also bumps the version. A failure rolls the whole file back.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent

_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """Raised when a migration file is invalid or fails to apply."""


class SchemaNotReadyError(RuntimeError):
    """Raised at startup when the database is not at the expected version."""


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return migration files ordered by version."""
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(f"invalid migration filename: {path.name}")
        migrations.append(Migration(version=int(match.group(1)), path=path))

    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration versions")
    return migrations


def latest_version(directory: Path = MIGRATIONS_DIR) -> int:
    migrations = discover(directory)
    return migrations[-1].version if migrations else 0


def current_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def pending(connection: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    current = current_version(connection)
    return [migration for migration in discover(directory) if migration.version > current]


def has_baseline(connection: sqlite3.Connection) -> bool:
    """True when the base ``memories`` table exists."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
    ).fetchone()
    return row is not None


def apply(connection: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every pending migration. Returns the versions that were applied."""
    applied: list[int] = []
    for migration in pending(connection, directory):
        sql = migration.path.read_text(encoding="utf-8")
        # The file must not contain its own transaction statements.
        script = f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {migration.version};\nCOMMIT;"
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            connection.rollback()
            # The filename is safe to report; the SQL and data are not.
            raise MigrationError(f"migration {migration.path.name} failed") from exc
        applied.append(migration.version)
    return applied


def not_ready_message(
    *,
    path: Path,
    version: int,
    latest: int,
    reason: str,
) -> str:
    """Build the operator-facing startup refusal message.

    Contains the database path and exact commands; never credentials or data.
    """
    if reason == "newer":
        guidance = (
            "This database was created by a newer build of RecallKit.\n"
            "Run that build, or restore a backup taken before the upgrade."
        )
    elif reason == "pending":
        guidance = (
            "Stop the server. Back up the database file and any -wal/-shm files,\n"
            "then run:\n"
            "  python -m app.cli migrate"
        )
    else:
        guidance = "For a brand-new database run:\n  python -m app.cli init"
    return (
        "Database schema is not ready.\n"
        f"  path:     {path}\n"
        f"  version:  {version}\n"
        f"  expected: {latest}\n"
        f"{guidance}"
    )
