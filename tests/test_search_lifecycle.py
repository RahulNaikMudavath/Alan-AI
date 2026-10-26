"""Active memories only in retrieval; history through the list filter."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

MakeMemory = Callable[..., dict[str, object]]

MODES = ["keyword", "semantic", "hybrid"]


def _supersede(client: TestClient, old_id: str, new_id: str, *, user_id: str = "user-a") -> None:
    response = client.post(
        f"/memories/{old_id}/supersede",
        json={"superseded_by": new_id, "reason": "user_correction"},
        params={"user_id": user_id},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("mode", MODES)
def test_ranked_search_excludes_superseded(
    client: TestClient, make_memory: MakeMemory, mode: str
) -> None:
    old = make_memory(content="Prefers dark mode.")  # content shared below
    new = make_memory(content="Prefers dark mode, but at night only.")
    _supersede(client, str(old["id"]), str(new["id"]))

    body = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "dark mode", "mode": mode},
    ).json()

    ids = [result["memory"]["id"] for result in body["results"]]
    assert old["id"] not in ids
    assert new["id"] in ids


@pytest.mark.parametrize("mode", MODES)
def test_coverage_counts_active_memories_only(
    client: TestClient, make_memory: MakeMemory, mode: str
) -> None:
    old = make_memory(content="Prefers dark mode.")
    new = make_memory(content="Prefers light mode.")
    _supersede(client, str(old["id"]), str(new["id"]))

    body = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "mode", "mode": mode},
    ).json()

    assert body["coverage"] == {"embedded": 1, "considered": 1}


def test_list_keyword_search_excludes_superseded(
    client: TestClient, make_memory: MakeMemory
) -> None:
    old = make_memory(content="Prefers dark mode.")
    new = make_memory(content="Prefers dark mode in the evening.")
    _supersede(client, str(old["id"]), str(new["id"]))

    body = client.get("/memories", params={"user_id": "user-a", "q": "dark"}).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [new["id"]]


def test_status_all_paginates_over_history(client: TestClient, make_memory: MakeMemory) -> None:
    first = make_memory(content="First fact.")
    second = make_memory(content="Second fact.")
    third = make_memory(content="Third fact.")
    _supersede(client, str(first["id"]), str(third["id"]))

    page_one = client.get(
        "/memories", params={"user_id": "user-a", "status": "all", "limit": 2}
    ).json()
    page_two = client.get(
        "/memories",
        params={"user_id": "user-a", "status": "all", "limit": 2, "offset": 2},
    ).json()

    assert page_one["total"] == page_two["total"] == 3
    ids = [item["id"] for item in page_one["items"] + page_two["items"]]
    assert set(ids) == {first["id"], second["id"], third["id"]}
    assert len(ids) == 3


def test_filters_compose_with_status(client: TestClient, make_memory: MakeMemory) -> None:
    old = make_memory(content="Old preference.", kind="preference", agent_id="agent-1")
    new = make_memory(content="New preference.", kind="preference", agent_id="agent-1")
    make_memory(content="A fact.", kind="fact", agent_id="agent-1")
    _supersede(client, str(old["id"]), str(new["id"]))

    body = client.get(
        "/memories",
        params={
            "user_id": "user-a",
            "status": "superseded",
            "kind": "preference",
            "agent_id": "agent-1",
        },
    ).json()

    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [old["id"]]


def test_other_users_superseded_memories_stay_invisible(
    client: TestClient, make_memory: MakeMemory
) -> None:
    old = make_memory(user_id="user-b", content="Old B fact.")
    new = make_memory(user_id="user-b", content="New B fact.")
    _supersede(client, str(old["id"]), str(new["id"]), user_id="user-b")

    body = client.get("/memories", params={"user_id": "user-a", "status": "all"}).json()

    assert body["total"] == 0
