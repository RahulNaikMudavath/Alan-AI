"""Explicit, versioned, transactional schema migrations."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.cli import main
from app.config import Settings
from app.db import SCHEMA_PATH, Database
from app.main import create_app
from app.migrations.runner import (
    MigrationError,
    SchemaNotReadyError,
    apply,
    current_version,
    latest_version,
)


def _baseline_only(path: Path) -> None:
    """Create the pre-M3.3 schema, as an existing installation would have it."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _columns(path: Path, table: str = "memories") -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return current_version(connection)


def test_init_creates_a_database_from_nothing(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    settings = Settings(_env_file=None, database_path=path)

    assert main(["init"], settings=settings) == 0

    assert path.exists()
    assert _version(path) == latest_version()
    assert {"status", "superseded_at", "superseded_by"} <= _columns(path)
    assert _columns(path, "memory_conflicts") >= {"id", "memory_a", "memory_b"}


def test_init_accepts_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    settings = Settings(_env_file=None, database_path=path)

    assert main(["init"], settings=settings) == 0
    assert _version(path) == latest_version()


def test_init_refuses_an_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    Database(path).initialize()
    settings = Settings(_env_file=None, database_path=path)

    assert main(["init"], settings=settings) == 1


def test_migrate_upgrades_a_populated_baseline(tmp_path: Path) -> None:
    path = tmp_path / "populated.db"
    _baseline_only(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO memories (id, user_id, content, kind, created_at, updated_at)
            VALUES ('m1', 'user-a', 'Prefers dark mode', 'preference', '2025-01-01', '2025-01-01')
            """
        )
    assert _version(path) == 0

    settings = Settings(_env_file=None, database_path=path)
    assert main(["migrate"], settings=settings) == 0

    assert _version(path) == latest_version()
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("SELECT content, status FROM memories WHERE id = 'm1'").fetchone()
    assert row == ("Prefers dark mode", "active")


def test_migrate_check_reports_pending_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "pending.db"
    _baseline_only(path)
    settings = Settings(_env_file=None, database_path=path)

    assert main(["migrate", "--check"], settings=settings) == 1
    assert _version(path) == 0
    assert "status" not in _columns(path)


def test_migrate_is_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "repeat.db"
    Database(path).initialize()
    settings = Settings(_env_file=None, database_path=path)

    assert main(["migrate"], settings=settings) == 0
    assert main(["migrate"], settings=settings) == 0
    assert _version(path) == latest_version()


def test_migrate_on_a_missing_database_points_at_init(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    settings = Settings(_env_file=None, database_path=path)

    assert main(["migrate"], settings=settings) == 1
    assert not path.exists()


def test_startup_refuses_a_pending_database_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "pending.db"
    _baseline_only(path)
    settings = Settings(_env_file=None, database_path=path)

    with pytest.raises(SchemaNotReadyError) as excinfo:
        with TestClient(create_app(settings)):
            pass

    message = str(excinfo.value)
    assert "app.cli migrate" in message
    assert "Back up" in message
    assert _version(path) == 0
    assert "status" not in _columns(path)


def test_startup_refuses_a_missing_database_without_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    settings = Settings(_env_file=None, database_path=path)

    with pytest.raises(SchemaNotReadyError) as excinfo:
        with TestClient(create_app(settings)):
            pass

    assert "app.cli init" in str(excinfo.value)
    assert not path.exists()


def test_startup_refuses_a_database_from_a_newer_build(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    Database(path).initialize()
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {latest_version() + 5}")
    settings = Settings(_env_file=None, database_path=path)

    with pytest.raises(SchemaNotReadyError, match="newer"):
        with TestClient(create_app(settings)):
            pass


def test_require_current_accepts_a_fresh_database(tmp_path: Path) -> None:
    path = tmp_path / "ready.db"
    database = Database(path)
    database.initialize()

    database.require_current()

    assert database.state().pending is False


def test_failed_migration_rolls_back_and_keeps_the_version(tmp_path: Path) -> None:
    path = tmp_path / "fail.db"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_ok.sql").write_text("CREATE TABLE t1 (id TEXT);", encoding="utf-8")

    with closing(sqlite3.connect(path)) as connection:
        assert apply(connection, migrations) == [1]
        assert current_version(connection) == 1

        (migrations / "0002_bad.sql").write_text(
            "CREATE TABLE t2 (id TEXT);\nINSERT INTO missing_table VALUES (1);",
            encoding="utf-8",
        )
        with pytest.raises(MigrationError, match="0002_bad.sql"):
            apply(connection, migrations)

        assert current_version(connection) == 1
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "t1" in tables
        assert "t2" not in tables
        # The connection remains usable after rollback.
        assert connection.execute("SELECT COUNT(*) FROM t1").fetchone()[0] == 0


def test_discovery_rejects_duplicate_versions(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (migrations / "0001_b.sql").write_text("SELECT 1;", encoding="utf-8")

    with closing(sqlite3.connect(tmp_path / "dup.db")) as connection:
        with pytest.raises(MigrationError, match="duplicate"):
            apply(connection, migrations)


def test_backfill_requires_a_current_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "stale.db"
    _baseline_only(path)
    settings = Settings(_env_file=None, database_path=path)

    exit_code = main(["backfill-embeddings"], settings=settings)

    assert exit_code == 1
    assert "app.cli migrate" in capsys.readouterr().out
