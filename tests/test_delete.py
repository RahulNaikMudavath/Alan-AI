"""Permanent deletion."""

import sqlite3
from collections.abc import Callable
from contextlib import closing

from fastapi.testclient import TestClient

from app.config import Settings

MakeMemory = Callable[..., dict[str, object]]


def _deleted(client: TestClient, memory_id: str, user_id: str = "user-a"):
    return client.delete(f"/memories/{memory_id}", params={"user_id": user_id})


def test_delete_returns_204_with_no_body(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(content="Temporary.")

    response = _deleted(client, str(memory["id"]))

    assert response.status_code == 204
    assert response.content == b""


def test_deleted_memory_is_no_longer_readable(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(content="Temporary.")

    _deleted(client, str(memory["id"]))

    response = client.get(f"/memories/{memory['id']}", params={"user_id": "user-a"})
    assert response.status_code == 404


def test_delete_removes_the_row_from_sqlite(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    memory = make_memory(content="Temporary.")

    _deleted(client, str(memory["id"]))

    with closing(sqlite3.connect(settings.database_path)) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM memories WHERE id = ?", (memory["id"],)
        ).fetchone()[0]
    assert remaining == 0


def test_deleted_memory_disappears_from_list_and_search(
    client: TestClient, make_memory: MakeMemory
) -> None:
    kept = make_memory(content="Dark mode is kept.")
    removed = make_memory(content="Dark mode is removed.")

    _deleted(client, str(removed["id"]))

    listing = client.get("/memories", params={"user_id": "user-a"}).json()
    assert [item["id"] for item in listing["items"]] == [kept["id"]]
    assert listing["total"] == 1

    search = client.get("/memories", params={"user_id": "user-a", "q": "dark"}).json()
    assert [item["id"] for item in search["items"]] == [kept["id"]]
    assert search["total"] == 1


def test_delete_returns_404_for_unknown_id(client: TestClient) -> None:
    response = _deleted(client, "does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}


def test_delete_is_not_idempotent(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(content="Temporary.")

    assert _deleted(client, str(memory["id"])).status_code == 204
    assert _deleted(client, str(memory["id"])).status_code == 404


def test_delete_cannot_touch_another_users_memory(
    client: TestClient, make_memory: MakeMemory
) -> None:
    original = make_memory(user_id="user-a", content="Owned by A.")

    response = _deleted(client, str(original["id"]), user_id="user-b")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    assert client.get(f"/memories/{original['id']}", params={"user_id": "user-a"}).json() == (
        original
    )


def test_delete_requires_user_id(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(content="Temporary.")

    assert client.delete(f"/memories/{memory['id']}").status_code == 422
