"""Lifecycle: explicit supersession, status filters, and edit guards."""

from collections.abc import Callable

from fastapi.testclient import TestClient

MakeMemory = Callable[..., dict[str, object]]


def _supersede(
    client: TestClient,
    old_id: str,
    new_id: str,
    *,
    user_id: str = "user-a",
    reason: str = "user_correction",
    note: str | None = "Changed preference",
):
    body = {"superseded_by": new_id, "reason": reason}
    if note is not None:
        body["note"] = note
    return client.post(f"/memories/{old_id}/supersede", json=body, params={"user_id": user_id})


def test_new_memory_is_active(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(content="Prefers dark mode.")

    assert memory["status"] == "active"
    assert memory["superseded_at"] is None
    assert memory["superseded_by"] is None
    assert memory["superseded_reason"] is None
    assert memory["superseded_note"] is None


def test_supersede_records_when_why_and_by_whom(
    client: TestClient, make_memory: MakeMemory
) -> None:
    old = make_memory(content="Prefers dark mode.")
    new = make_memory(content="Prefers light mode.")

    response = _supersede(client, str(old["id"]), str(new["id"]), note=" corrected ")

    assert response.status_code == 200
    body = response.json()
    assert body["superseded"]["id"] == old["id"]
    assert body["superseded"]["status"] == "superseded"
    assert body["superseded"]["superseded_by"] == new["id"]
    assert body["superseded"]["superseded_reason"] == "user_correction"
    assert body["superseded"]["superseded_note"] == "corrected"
    assert body["superseded"]["superseded_at"] is not None
    assert body["replacement"]["id"] == new["id"]
    assert body["replacement"]["status"] == "active"


def test_superseded_memory_is_still_readable(client: TestClient, make_memory: MakeMemory) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    _supersede(client, str(old["id"]), str(new["id"]))

    response = client.get(f"/memories/{old['id']}", params={"user_id": "user-a"})

    assert response.status_code == 200
    assert response.json()["status"] == "superseded"


def test_list_status_filters(client: TestClient, make_memory: MakeMemory) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    _supersede(client, str(old["id"]), str(new["id"]))

    active = client.get("/memories", params={"user_id": "user-a"}).json()
    superseded = client.get(
        "/memories", params={"user_id": "user-a", "status": "superseded"}
    ).json()
    everything = client.get("/memories", params={"user_id": "user-a", "status": "all"}).json()

    assert active["total"] == 1
    assert [item["id"] for item in active["items"]] == [new["id"]]
    assert superseded["total"] == 1
    assert [item["id"] for item in superseded["items"]] == [old["id"]]
    assert everything["total"] == 2


def test_patch_rejects_a_superseded_memory(client: TestClient, make_memory: MakeMemory) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    _supersede(client, str(old["id"]), str(new["id"]))

    response = client.patch(
        f"/memories/{old['id']}", json={"content": "Rewritten"}, params={"user_id": "user-a"}
    )

    assert response.status_code == 409
    stored = client.get(f"/memories/{old['id']}", params={"user_id": "user-a"}).json()
    assert stored["content"] == "Old fact."


def test_supersede_rejects_self(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(content="A fact.")

    response = _supersede(client, str(memory["id"]), str(memory["id"]))

    assert response.status_code == 409


def test_supersede_rejects_an_already_superseded_target(
    client: TestClient, make_memory: MakeMemory
) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    third = make_memory(content="Third fact.")
    assert _supersede(client, str(old["id"]), str(new["id"])).status_code == 200

    response = _supersede(client, str(old["id"]), str(third["id"]))

    assert response.status_code == 409


def test_supersede_rejects_an_inactive_replacement(
    client: TestClient, make_memory: MakeMemory
) -> None:
    first = make_memory(content="First fact.")
    second = make_memory(content="Second fact.")
    third = make_memory(content="Third fact.")
    assert _supersede(client, str(second["id"]), str(third["id"])).status_code == 200

    response = _supersede(client, str(first["id"]), str(second["id"]))

    assert response.status_code == 409


def test_supersede_cannot_touch_another_users_memory(
    client: TestClient, make_memory: MakeMemory
) -> None:
    mine = make_memory(user_id="user-a", content="Mine.")
    theirs = make_memory(user_id="user-b", content="Theirs.")

    response = _supersede(client, str(mine["id"]), str(theirs["id"]), user_id="user-a")

    assert response.status_code == 404
    assert (
        client.get(f"/memories/{mine['id']}", params={"user_id": "user-a"}).json()["status"]
        == "active"
    )


def test_supersede_requires_a_valid_reason(client: TestClient, make_memory: MakeMemory) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")

    missing = client.post(
        f"/memories/{old['id']}/supersede",
        json={"superseded_by": new["id"]},
        params={"user_id": "user-a"},
    )
    invalid = client.post(
        f"/memories/{old['id']}/supersede",
        json={"superseded_by": new["id"], "reason": "because"},
        params={"user_id": "user-a"},
    )

    assert missing.status_code == 422
    assert invalid.status_code == 422


def test_superseded_memory_can_be_deleted(client: TestClient, make_memory: MakeMemory) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    _supersede(client, str(old["id"]), str(new["id"]))

    assert client.delete(f"/memories/{old['id']}", params={"user_id": "user-a"}).status_code == 204
    assert client.get(f"/memories/{old['id']}", params={"user_id": "user-a"}).status_code == 404


def test_deleting_the_replacement_clears_the_pointer(
    client: TestClient, make_memory: MakeMemory
) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    _supersede(client, str(old["id"]), str(new["id"]))

    assert client.delete(f"/memories/{new['id']}", params={"user_id": "user-a"}).status_code == 204

    stored = client.get(f"/memories/{old['id']}", params={"user_id": "user-a"}).json()
    assert stored["status"] == "superseded"
    assert stored["superseded_by"] is None
    assert stored["superseded_reason"] == "user_correction"
    assert stored["superseded_at"] is not None
