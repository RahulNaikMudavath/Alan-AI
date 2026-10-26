"""Data isolation between users."""

from fastapi.testclient import TestClient


def _create(client: TestClient, user_id: str, content: str) -> dict[str, object]:
    response = client.post("/memories", json={"user_id": user_id, "content": content})
    assert response.status_code == 201
    return response.json()


def test_other_user_cannot_read_a_memory(client: TestClient) -> None:
    memory = _create(client, "user-a", "My API key rotates monthly.")

    response = client.get(f"/memories/{memory['id']}", params={"user_id": "user-b"})

    assert response.status_code == 404


def test_foreign_memory_is_indistinguishable_from_a_missing_one(client: TestClient) -> None:
    memory = _create(client, "user-a", "My API key rotates monthly.")

    foreign = client.get(f"/memories/{memory['id']}", params={"user_id": "user-b"})
    missing = client.get("/memories/00000000000000000000000000000000", params={"user_id": "user-b"})

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_each_user_reads_only_their_own_memory(client: TestClient) -> None:
    memory_a = _create(client, "user-a", "I am user A.")
    memory_b = _create(client, "user-b", "I am user B.")

    assert memory_a["id"] != memory_b["id"]
    assert client.get(f"/memories/{memory_a['id']}", params={"user_id": "user-a"}).json() == (
        memory_a
    )
    assert client.get(f"/memories/{memory_b['id']}", params={"user_id": "user-b"}).json() == (
        memory_b
    )
