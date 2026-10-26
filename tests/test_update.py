"""Partial updates: what changes, what does not, and who is allowed."""

from collections.abc import Callable
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

MakeMemory = Callable[..., dict[str, object]]


def _patched(client: TestClient, memory_id: str, body: dict[str, object], user_id: str = "user-a"):
    return client.patch(f"/memories/{memory_id}", json=body, params={"user_id": user_id})


def test_update_content_keeps_everything_else(client: TestClient, make_memory: MakeMemory) -> None:
    original = make_memory(content="Old content.", kind="preference", agent_id="agent-1")

    response = _patched(client, str(original["id"]), {"content": "New content."})

    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "New content."
    assert body["kind"] == "preference"
    assert body["agent_id"] == "agent-1"
    assert body["user_id"] == "user-a"
    assert body["created_at"] == original["created_at"]
    assert datetime.fromisoformat(body["updated_at"]) > datetime.fromisoformat(
        str(original["updated_at"])
    )


def test_update_kind_keeps_content(client: TestClient, make_memory: MakeMemory) -> None:
    original = make_memory(content="Kept content.", kind="fact")

    body = _patched(client, str(original["id"]), {"kind": "task"}).json()

    assert body["content"] == "Kept content."
    assert body["kind"] == "task"
    assert datetime.fromisoformat(body["updated_at"]) > datetime.fromisoformat(
        str(original["updated_at"])
    )


def test_update_both_fields(client: TestClient, make_memory: MakeMemory) -> None:
    original = make_memory(content="Old.", kind="fact")

    body = _patched(client, str(original["id"]), {"content": "New.", "kind": "event"}).json()

    assert body["content"] == "New."
    assert body["kind"] == "event"


def test_update_trims_content(client: TestClient, make_memory: MakeMemory) -> None:
    original = make_memory(content="Old.")

    body = _patched(client, str(original["id"]), {"content": "  padded  "}).json()

    assert body["content"] == "padded"


def test_update_refreshes_timestamp_even_when_values_are_unchanged(
    client: TestClient, make_memory: MakeMemory
) -> None:
    original = make_memory(content="Same content.", kind="fact")

    body = _patched(
        client, str(original["id"]), {"content": "Same content.", "kind": "fact"}
    ).json()

    assert datetime.fromisoformat(body["updated_at"]) > datetime.fromisoformat(
        str(original["updated_at"])
    )


def test_update_is_visible_on_the_next_read(client: TestClient, make_memory: MakeMemory) -> None:
    original = make_memory(content="Old.")

    _patched(client, str(original["id"]), {"content": "New."})

    read = client.get(f"/memories/{original['id']}", params={"user_id": "user-a"}).json()
    assert read["content"] == "New."


def test_update_reindexes_search(client: TestClient, make_memory: MakeMemory) -> None:
    original = make_memory(content="Original text about kubernetes.")

    _patched(client, str(original["id"]), {"content": "Replacement text about terraform."})

    assert (
        client.get("/memories", params={"user_id": "user-a", "q": "terraform"}).json()["total"] == 1
    )
    assert (
        client.get("/memories", params={"user_id": "user-a", "q": "kubernetes"}).json()["total"]
        == 0
    )


def test_update_returns_404_for_unknown_id(client: TestClient) -> None:
    response = _patched(client, "does-not-exist", {"content": "New."})

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}


def test_update_cannot_touch_another_users_memory(
    client: TestClient, make_memory: MakeMemory
) -> None:
    original = make_memory(user_id="user-a", content="Owned by A.")

    response = _patched(client, str(original["id"]), {"content": "Hijacked."}, user_id="user-b")

    assert response.status_code == 404
    assert response.json() == {"detail": "Memory not found"}
    # The row is untouched and still readable by its real owner.
    unchanged = client.get(f"/memories/{original['id']}", params={"user_id": "user-a"}).json()
    assert unchanged == original


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({}, "empty body"),
        ({"content": None}, "null content"),
        ({"kind": None}, "null kind"),
        ({"content": "   "}, "blank content"),
        ({"content": ""}, "empty content"),
        ({"content": "x" * 8001}, "oversized content"),
        ({"kind": "gossip"}, "unknown kind"),
        ({"content": "ok", "user_id": "user-b"}, "extra field"),
    ],
)
def test_update_rejects_invalid_bodies(
    client: TestClient, make_memory: MakeMemory, body: dict[str, object], reason: str
) -> None:
    original = make_memory(content="Untouched.")

    response = _patched(client, str(original["id"]), body)

    assert response.status_code == 422, reason
    assert (
        client.get(f"/memories/{original['id']}", params={"user_id": "user-a"}).json() == original
    )


def test_update_requires_user_id(client: TestClient, make_memory: MakeMemory) -> None:
    original = make_memory(content="Old.")

    response = client.patch(f"/memories/{original['id']}", json={"content": "New."})

    assert response.status_code == 422
