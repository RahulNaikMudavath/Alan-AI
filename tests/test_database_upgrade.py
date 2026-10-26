"""Upgrading a Milestone 2 database to the Milestone 3 schema."""

import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app

# The schema exactly as it existed before memory_embeddings was added. Kept as a
# literal snapshot on purpose: this test is about upgrading real old files.
MILESTONE_2_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    agent_id   TEXT,
    content    TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'fact',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(content)) > 0),
    CHECK (length(user_id) > 0)
);
CREATE INDEX IF NOT EXISTS idx_memories_user_created
    ON memories (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_user_agent
    ON memories (user_id, agent_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    memory_id UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts (memory_id, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
    INSERT INTO memories_fts (memory_id, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
END;
INSERT INTO memories_fts (memory_id, content)
SELECT m.id, m.content
FROM memories m
WHERE NOT EXISTS (SELECT 1 FROM memories_fts f WHERE f.memory_id = m.id);
"""


def _create_milestone_2_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(MILESTONE_2_SCHEMA)
        connection.execute(
            """
            INSERT INTO memories (id, user_id, agent_id, content, kind, created_at, updated_at)
            VALUES ('legacy-1', 'user-a', 'agent-1', 'Legacy note about kubernetes.',
                    'fact', '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00')
            """
        )


def test_milestone_2_database_has_no_embedding_table(tmp_path: Path) -> None:
    path = tmp_path / "m2.db"
    _create_milestone_2_database(path)

    with closing(sqlite3.connect(path)) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}

    assert "memories" in names
    assert "memories_fts" in names
    assert "memory_embeddings" not in names


def test_initialize_upgrades_without_losing_data(tmp_path: Path) -> None:
    path = tmp_path / "m2.db"
    _create_milestone_2_database(path)

    database = Database(path)
    database.initialize()
    database.initialize()  # idempotent: a second startup must be harmless

    with closing(database.connect()) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        memory = connection.execute(
            "SELECT content, user_id, agent_id FROM memories WHERE id = 'legacy-1'"
        ).fetchone()
        searchable = connection.execute(
            "SELECT memory_id FROM memories_fts WHERE memories_fts MATCH ?", ('"kubernetes"*',)
        ).fetchall()
        vectors = connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]

    assert "memory_embeddings" in tables
    assert tuple(memory) == ("Legacy note about kubernetes.", "user-a", "agent-1")
    assert [row[0] for row in searchable] == ["legacy-1"]
    # The upgrade must not invent vectors for memories it never embedded.
    assert vectors == 0


def test_upgraded_database_enforces_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "m2.db"
    _create_milestone_2_database(path)
    database = Database(path)
    database.initialize()

    with closing(database.connect()) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_app_runs_against_an_upgraded_database(tmp_path: Path) -> None:
    path = tmp_path / "m2.db"
    _create_milestone_2_database(path)
    settings = Settings(_env_file=None, database_path=path)

    with TestClient(create_app(settings)) as client:
        listing = client.get("/memories", params={"user_id": "user-a"}).json()
        semantic = client.post(
            "/memories/search",
            json={"user_id": "user-a", "query": "kubernetes", "mode": "semantic"},
        ).json()
        created = client.post(
            "/memories", json={"user_id": "user-a", "content": "New note about terraform."}
        )
        keyword = client.post(
            "/memories/search",
            json={"user_id": "user-a", "query": "terraform", "mode": "semantic"},
        ).json()

    assert listing["total"] == 1
    assert listing["items"][0]["id"] == "legacy-1"

    # The legacy row is searchable by keyword but honestly reported as having
    # no vector, so semantic search cannot invent a match.
    assert semantic["coverage"] == {"embedded": 0, "considered": 1}
    assert semantic["results"] == []
    assert semantic["degraded"] is False

    assert created.status_code == 201
    assert [result["memory"]["id"] for result in keyword["results"]] == [created.json()["id"]]
    assert keyword["coverage"] == {"embedded": 1, "considered": 2}


def test_delete_cascade_works_on_an_upgraded_database(tmp_path: Path) -> None:
    path = tmp_path / "m2.db"
    _create_milestone_2_database(path)
    settings = Settings(_env_file=None, database_path=path)

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/memories", json={"user_id": "user-a", "content": "Will be deleted."}
        ).json()
        assert (
            client.delete(f"/memories/{created['id']}", params={"user_id": "user-a"}).status_code
            == 204
        )

    with closing(sqlite3.connect(path)) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?", (created["id"],)
        ).fetchone()[0]

    assert remaining == 0
