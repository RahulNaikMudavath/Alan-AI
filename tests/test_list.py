"""Listing, filtering, ordering and pagination."""

from collections.abc import Callable

from fastapi.testclient import TestClient

MakeMemory = Callable[..., dict[str, object]]


def test_empty_list_returns_an_envelope(client: TestClient) -> None:
    response = client.get("/memories", params={"user_id": "user-a"})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_list_is_scoped_to_the_requesting_user(client: TestClient, make_memory: MakeMemory) -> None:
    mine = make_memory(user_id="user-a", content="Mine.")
    make_memory(user_id="user-b", content="Not mine.")

    body = client.get("/memories", params={"user_id": "user-a"}).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [mine["id"]]


def test_list_is_newest_first(client: TestClient, make_memory: MakeMemory) -> None:
    first = make_memory(content="First.")
    second = make_memory(content="Second.")
    third = make_memory(content="Third.")

    body = client.get("/memories", params={"user_id": "user-a"}).json()

    assert [item["id"] for item in body["items"]] == [third["id"], second["id"], first["id"]]


def test_list_filters_by_agent(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(content="From agent one.", agent_id="agent-1")
    from_two = make_memory(content="From agent two.", agent_id="agent-2")
    make_memory(content="From nobody.")

    body = client.get("/memories", params={"user_id": "user-a", "agent_id": "agent-2"}).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [from_two["id"]]


def test_list_filters_by_kind(client: TestClient, make_memory: MakeMemory) -> None:
    fact = make_memory(content="A fact.", kind="fact")
    make_memory(content="A preference.", kind="preference")

    body = client.get("/memories", params={"user_id": "user-a", "kind": "fact"}).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [fact["id"]]


def test_list_combines_filters(client: TestClient, make_memory: MakeMemory) -> None:
    wanted = make_memory(content="Wanted.", kind="task", agent_id="agent-1")
    make_memory(content="Wrong agent.", kind="task", agent_id="agent-2")
    make_memory(content="Wrong kind.", kind="fact", agent_id="agent-1")

    body = client.get(
        "/memories",
        params={"user_id": "user-a", "kind": "task", "agent_id": "agent-1"},
    ).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [wanted["id"]]


def test_pagination_walks_every_memory_once(client: TestClient, make_memory: MakeMemory) -> None:
    created = [make_memory(content=f"Memory {index}.") for index in range(5)]
    expected = [item["id"] for item in reversed(created)]

    seen: list[str] = []
    for offset in (0, 2, 4):
        body = client.get(
            "/memories", params={"user_id": "user-a", "limit": 2, "offset": offset}
        ).json()
        assert body["total"] == 5
        assert body["limit"] == 2
        assert body["offset"] == offset
        seen.extend(item["id"] for item in body["items"])

    assert seen == expected
    assert len(set(seen)) == 5


def test_offset_past_the_end_returns_no_items_but_keeps_total(
    client: TestClient, make_memory: MakeMemory
) -> None:
    make_memory(content="Only one.")

    body = client.get("/memories", params={"user_id": "user-a", "offset": 10}).json()

    assert body["items"] == []
    assert body["total"] == 1


def test_limit_larger_than_result_set_returns_everything(
    client: TestClient, make_memory: MakeMemory
) -> None:
    make_memory(content="One.")
    make_memory(content="Two.")

    body = client.get("/memories", params={"user_id": "user-a", "limit": 100}).json()

    assert len(body["items"]) == 2
    assert body["total"] == 2


def test_list_requires_user_id(client: TestClient) -> None:
    assert client.get("/memories").status_code == 422


def test_list_rejects_invalid_paging(client: TestClient) -> None:
    assert client.get("/memories", params={"user_id": "user-a", "limit": 0}).status_code == 422
    assert client.get("/memories", params={"user_id": "user-a", "limit": 101}).status_code == 422
    assert client.get("/memories", params={"user_id": "user-a", "offset": -1}).status_code == 422


def test_list_rejects_unknown_kind(client: TestClient) -> None:
    response = client.get("/memories", params={"user_id": "user-a", "kind": "gossip"})

    assert response.status_code == 422
