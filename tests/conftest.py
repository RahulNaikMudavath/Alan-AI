"""Shared fixtures.

Every test gets a brand-new SQLite file inside pytest's ``tmp_path``. The
fixture passes explicit settings to ``create_app``, so no test can reach the
development database at ``data/recallkit.db``.

The suite is also offline by construction: an autouse fixture replaces
``socket.create_connection`` with a guard, so any accidental outbound connection
fails the test that attempted it.
"""

import socket
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.repositories.embeddings import EmbeddingRepository
from app.retrieval.vectors import pack_vector


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens an outbound network connection."""

    def guard(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is not allowed in tests")

    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # ignore a developer's .env during tests
        database_path=tmp_path / "recallkit-test.db",
        environment="test",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    # The context manager runs startup/shutdown, which creates the schema.
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def query_db(settings: Settings) -> Callable[..., list[tuple]]:
    """Run read-only SQL against the test database, outside the app."""

    def _query(sql: str, params: tuple[object, ...] = ()) -> list[tuple]:
        with closing(sqlite3.connect(settings.database_path)) as connection:
            return connection.execute(sql, params).fetchall()

    return _query


@pytest.fixture
def store_vector(settings: Settings) -> Callable[..., None]:
    """Write a vector directly, as if a different app version had indexed it."""

    def _store(
        memory_id: str,
        vector: list[float],
        *,
        model: str = "fixed-v1",
    ) -> None:
        database = Database(settings.database_path)
        with closing(database.connect()) as connection:
            EmbeddingRepository(connection).upsert(
                memory_id=memory_id,
                model=model,
                dimensions=len(vector),
                vector=pack_vector(vector),
                created_at="2025-01-01T00:00:00+00:00",
            )

    return _store


@pytest.fixture
def memory_payload() -> dict[str, object]:
    return {
        "user_id": "user-a",
        "content": "Prefers dark mode and concise answers.",
        "kind": "preference",
    }


@pytest.fixture
def make_memory(client: TestClient) -> Callable[..., dict[str, object]]:
    """Create a memory through the API and return the stored representation.

    A failed create fails the test immediately, so tests that use this fixture
    only have to assert the behaviour they are actually about.
    """

    def _make(
        *,
        user_id: str = "user-a",
        content: str = "A memory.",
        kind: str = "fact",
        agent_id: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"user_id": user_id, "content": content, "kind": kind}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        response = client.post("/memories", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make
