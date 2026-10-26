"""Vectors are written, refreshed, removed, and backfilled correctly."""

import sqlite3
from collections.abc import Callable
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.llm.base import EmbeddingError
from app.repositories.embeddings import EmbeddingRepository
from app.retrieval.vectors import pack_vector

MakeMemory = Callable[..., dict[str, object]]


class ExplodingProvider:
    """Provider that always fails, to prove writes survive an outage."""

    model = "exploding-v1"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("provider is down")

    def close(self) -> None:
        """Nothing to release."""


def _embedding_rows(settings: Settings, memory_id: str) -> list[tuple[str, int, bytes]]:
    with closing(sqlite3.connect(settings.database_path)) as connection:
        return connection.execute(
            "SELECT model, dimensions, vector FROM memory_embeddings WHERE memory_id = ?",
            (memory_id,),
        ).fetchall()


def _insert_memory(
    settings: Settings,
    *,
    memory_id: str,
    user_id: str = "user-a",
    content: str = "A legacy memory.",
    kind: str = "fact",
) -> None:
    """Write a memory without going through the API, so no vector is created."""
    Database(settings.database_path).initialize()
    with closing(sqlite3.connect(settings.database_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO memories (id, user_id, agent_id, content, kind, created_at, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                memory_id,
                user_id,
                content,
                kind,
                "2025-01-01T00:00:00+00:00",
                "2025-01-01T00:00:00+00:00",
            ),
        )


def _store_vector(settings: Settings, *, memory_id: str, model: str, vector: list[float]) -> None:
    database = Database(settings.database_path)
    with closing(database.connect()) as connection:
        EmbeddingRepository(connection).upsert(
            memory_id=memory_id,
            model=model,
            dimensions=len(vector),
            vector=pack_vector(vector),
            created_at="2025-01-01T00:00:00+00:00",
        )


def test_create_embeds_the_memory(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    memory = make_memory(content="Prefers dark mode.")

    rows = _embedding_rows(settings, str(memory["id"]))

    assert len(rows) == 1
    model, dimensions, blob = rows[0]
    assert model == "recallkit-fake-hash-v1-256"
    assert dimensions == 256
    assert len(blob) == 256 * 4


def test_kind_only_update_keeps_the_vector(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    memory = make_memory(content="Prefers dark mode.")
    before = _embedding_rows(settings, str(memory["id"]))

    response = client.patch(
        f"/memories/{memory['id']}", json={"kind": "preference"}, params={"user_id": "user-a"}
    )

    assert response.status_code == 200
    assert _embedding_rows(settings, str(memory["id"])) == before


def test_content_update_replaces_the_vector(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    memory = make_memory(content="Prefers dark mode.")
    before = _embedding_rows(settings, str(memory["id"]))

    response = client.patch(
        f"/memories/{memory['id']}",
        json={"content": "Prefers light mode now."},
        params={"user_id": "user-a"},
    )

    assert response.status_code == 200
    after = _embedding_rows(settings, str(memory["id"]))
    assert len(after) == 1
    assert after[0][0] == before[0][0]
    assert after[0][2] != before[0][2]


def test_create_survives_an_embedding_outage(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client.app.state, "embedding_provider", ExplodingProvider())

    response = client.post("/memories", json={"user_id": "user-a", "content": "Still stored."})

    assert response.status_code == 201
    memory_id = response.json()["id"]
    assert _embedding_rows(settings, memory_id) == []
    # The memory itself is readable and listed.
    assert client.get(f"/memories/{memory_id}", params={"user_id": "user-a"}).status_code == 200
    assert client.get("/memories", params={"user_id": "user-a"}).json()["total"] == 1


def test_failed_reindex_removes_the_stale_vector(
    client: TestClient, settings: Settings, make_memory: MakeMemory, monkeypatch
) -> None:
    memory = make_memory(content="Prefers dark mode.")
    assert len(_embedding_rows(settings, str(memory["id"]))) == 1

    monkeypatch.setattr(client.app.state, "embedding_provider", ExplodingProvider())
    response = client.patch(
        f"/memories/{memory['id']}",
        json={"content": "Now prefers light mode."},
        params={"user_id": "user-a"},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "Now prefers light mode."
    # A stale vector describing the old text must not survive.
    assert _embedding_rows(settings, str(memory["id"])) == []


def test_delete_cascades_to_vectors(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    memory = make_memory(content="Temporary.")
    assert len(_embedding_rows(settings, str(memory["id"]))) == 1

    assert (
        client.delete(f"/memories/{memory['id']}", params={"user_id": "user-a"}).status_code == 204
    )

    assert _embedding_rows(settings, str(memory["id"])) == []


def test_backfill_embeds_missing_memories_and_is_idempotent(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.cli import main

    _insert_memory(settings, memory_id="legacy-1", content="Legacy dark mode note.")
    _insert_memory(settings, memory_id="legacy-2", content="Legacy coffee note.")

    first = main(["backfill-embeddings", "--batch-size", "1"], settings=settings)
    first_output = capsys.readouterr().out

    assert first == 0
    assert "embedded=2" in first_output
    assert len(_embedding_rows(settings, "legacy-1")) == 1
    assert len(_embedding_rows(settings, "legacy-2")) == 1

    second = main(["backfill-embeddings"], settings=settings)
    second_output = capsys.readouterr().out

    assert second == 0
    assert "embedded=0" in second_output


def test_backfill_can_be_scoped_to_one_user(settings: Settings) -> None:
    from app.cli import main

    _insert_memory(settings, memory_id="a-1", user_id="user-a")
    _insert_memory(settings, memory_id="b-1", user_id="user-b")

    assert main(["backfill-embeddings", "--user-id", "user-a"], settings=settings) == 0

    assert len(_embedding_rows(settings, "a-1")) == 1
    assert _embedding_rows(settings, "b-1") == []


def test_backfill_never_overwrites_another_models_vectors(settings: Settings) -> None:
    from app.cli import main

    _insert_memory(settings, memory_id="legacy-1", content="Legacy note.")
    _store_vector(settings, memory_id="legacy-1", model="other-model", vector=[0.1, 0.2, 0.3, 0.4])

    assert main(["backfill-embeddings"], settings=settings) == 0

    with closing(sqlite3.connect(settings.database_path)) as connection:
        rows = dict(
            connection.execute(
                "SELECT model, vector FROM memory_embeddings WHERE memory_id = 'legacy-1'"
            ).fetchall()
        )
    assert set(rows) == {"other-model", "recallkit-fake-hash-v1-256"}
    assert rows["other-model"] == pack_vector([0.1, 0.2, 0.3, 0.4])


def test_backfill_reports_disabled_embeddings(settings: Settings) -> None:
    from app.cli import main

    disabled = Settings(
        _env_file=None,
        database_path=settings.database_path,
        embedding_provider="none",
    )

    assert main(["backfill-embeddings"], settings=disabled) == 2


def test_backfilled_memory_becomes_semantically_searchable(
    client: TestClient, settings: Settings
) -> None:
    from app.cli import main

    _insert_memory(settings, memory_id="legacy-1", content="Owns a kubernetes cluster.")

    before = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "kubernetes", "mode": "semantic"},
    ).json()
    assert before["coverage"] == {"embedded": 0, "considered": 1}
    assert before["results"] == []

    assert main(["backfill-embeddings"], settings=settings) == 0

    after = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "kubernetes", "mode": "semantic"},
    ).json()
    assert after["coverage"] == {"embedded": 1, "considered": 1}
    assert [result["memory"]["id"] for result in after["results"]] == ["legacy-1"]


def test_insert_if_absent_never_overwrites_an_existing_vector(settings: Settings) -> None:
    _insert_memory(settings, memory_id="m-1")
    database = Database(settings.database_path)
    with closing(database.connect()) as connection:
        repository = EmbeddingRepository(connection)
        first = repository.insert_if_absent(
            memory_id="m-1",
            model="m",
            dimensions=2,
            vector=pack_vector([1.0, 0.0]),
            created_at="2025-01-01T00:00:00+00:00",
        )
        second = repository.insert_if_absent(
            memory_id="m-1",
            model="m",
            dimensions=2,
            vector=pack_vector([0.0, 1.0]),
            created_at="2025-01-02T00:00:00+00:00",
        )
        stored = repository.get("m-1", model="m")

    assert first is True
    assert second is False
    assert stored == (2, pack_vector([1.0, 0.0]))


def test_upsert_replaces_a_vector_on_reindex(settings: Settings) -> None:
    _insert_memory(settings, memory_id="m-1")
    database = Database(settings.database_path)
    with closing(database.connect()) as connection:
        repository = EmbeddingRepository(connection)
        repository.upsert(
            memory_id="m-1",
            model="m",
            dimensions=2,
            vector=pack_vector([1.0, 0.0]),
            created_at="2025-01-01T00:00:00+00:00",
        )
        repository.upsert(
            memory_id="m-1",
            model="m",
            dimensions=2,
            vector=pack_vector([0.0, 1.0]),
            created_at="2025-01-02T00:00:00+00:00",
        )
        stored = repository.get("m-1", model="m")

    assert stored == (2, pack_vector([0.0, 1.0]))
