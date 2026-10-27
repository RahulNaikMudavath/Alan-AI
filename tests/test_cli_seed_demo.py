"""Explicit, temporary-database demo seeding for the dashboard."""

import sqlite3
from contextlib import closing
from pathlib import Path

from app.cli import main
from app.config import Settings


def _counts(settings: Settings) -> dict[str, int]:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        tables = [
            "memories",
            "memory_embeddings",
            "memory_sources",
            "conversations",
            "memory_conflicts",
        ]
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }


def test_seed_demo_creates_a_complete_demo(settings: Settings) -> None:
    assert main(["seed-demo"], settings=settings) == 0

    counts = _counts(settings)
    assert counts["memories"] == 10
    assert counts["memory_embeddings"] == 10
    assert counts["memory_sources"] == 1
    assert counts["conversations"] == 1
    assert counts["memory_conflicts"] == 1

    with closing(sqlite3.connect(settings.database_path)) as connection:
        statuses = dict(
            connection.execute("SELECT status, COUNT(*) FROM memories GROUP BY status").fetchall()
        )
        superseded = connection.execute(
            "SELECT content, superseded_by, superseded_reason "
            "FROM memories WHERE status = 'superseded'"
        ).fetchone()
        replacement = connection.execute(
            "SELECT content FROM memories WHERE id = ?", (superseded[1],)
        ).fetchone()

    assert statuses == {"active": 9, "superseded": 1}
    assert superseded[0] == "Prefers dark mode in the editor."
    assert superseded[2] == "user_correction"
    assert replacement[0] == "Prefers light mode in the editor."


def test_seed_demo_is_idempotent_for_the_same_user(settings: Settings) -> None:
    assert main(["seed-demo"], settings=settings) == 0
    before = _counts(settings)

    assert main(["seed-demo"], settings=settings) == 0
    after = _counts(settings)

    assert before == after


def test_seed_demo_supports_another_user(settings: Settings) -> None:
    assert main(["seed-demo", "--user-id", "someone-else"], settings=settings) == 0

    with closing(sqlite3.connect(settings.database_path)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = 'someone-else'"
        ).fetchone()[0]

    assert row == 10


def test_seed_demo_refuses_a_stale_schema(tmp_path: Path) -> None:
    stale = Settings(
        _env_file=None,
        database_path=tmp_path / "stale.db",
        environment="test",
    )
    # Create the file but no schema; require_current refuses.
    database_path = stale.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_path.touch()

    assert main(["seed-demo"], settings=stale) == 1
    assert database_path.exists()


def test_seed_demo_does_not_create_a_conversation_message(
    settings: Settings,
) -> None:
    assert main(["seed-demo"], settings=settings) == 0

    with closing(sqlite3.connect(settings.database_path)) as connection:
        messages = connection.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]

    assert messages == 0
