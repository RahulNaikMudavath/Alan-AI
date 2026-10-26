"""Create and read a memory, plus input validation."""

import sqlite3
from datetime import datetime

from fastapi.testclient import TestClient

from app.config import Settings


def test_create_returns_the_stored_memory(
    client: TestClient, memory_payload: dict[str, object]
) -> None:
    response = client.post("/memories", json=memory_payload)

    assert response.status_code == 201
    body = response.json()
    assert len(body["id"]) == 32
    assert body["user_id"] == "user-a"
    assert body["content"] == "Prefers dark mode and concise answers."
    assert body["kind"] == "preference"
    assert body["agent_id"] is None
    # Timestamps are timezone-aware and identical on creation.
    assert datetime.fromisoformat(body["created_at"]).tzinfo is not None
    assert body["created_at"] == body["updated_at"]


def test_create_trims_surrounding_whitespace(client: TestClient) -> None:
    response = client.post(
        "/memories",
        json={"user_id": " user-a ", "content": "  Likes tea  ", "agent_id": " agent-1 "},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == "user-a"
    assert body["content"] == "Likes tea"
    assert body["agent_id"] == "agent-1"


def test_created_memory_is_written_to_sqlite(
    client: TestClient, settings: Settings, memory_payload: dict[str, object]
) -> None:
    created = client.post("/memories", json=memory_payload).json()

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT user_id, content, kind FROM memories WHERE id = ?", (created["id"],)
        ).fetchone()

    assert row == ("user-a", "Prefers dark mode and concise answers.", "preference")


def test_get_returns_the_memory_for_its_owner(
    client: TestClient, memory_payload: dict[str, object]
) -> None:
    created = client.post("/memories", json=memory_payload).json()

    response = client.get(f"/memories/{created['id']}", params={"user_id": "user-a"})

    assert response.status_code == 200
    assert response.json() == created


def test_get_returns_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/memories/does-not-exist", params={"user_id": "user-a"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}


def test_get_requires_a_user_id(client: TestClient, memory_payload: dict[str, object]) -> None:
    created = client.post("/memories", json=memory_payload).json()

    response = client.get(f"/memories/{created['id']}")

    assert response.status_code == 422


def test_create_rejects_blank_content(client: TestClient) -> None:
    response = client.post("/memories", json={"user_id": "user-a", "content": "   "})

    assert response.status_code == 422


def test_create_rejects_unknown_fields(client: TestClient) -> None:
    # A typo in "user_id" must fail loudly instead of storing an unowned row.
    response = client.post("/memories", json={"userid": "user-a", "content": "Hello"})

    assert response.status_code == 422


def test_create_rejects_unknown_kind(client: TestClient) -> None:
    response = client.post(
        "/memories", json={"user_id": "user-a", "content": "Hello", "kind": "gossip"}
    )

    assert response.status_code == 422
