"""Every retrieval mode is scoped by user, agent, and kind."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

MakeMemory = Callable[..., dict[str, object]]

MODES = ["keyword", "semantic", "hybrid"]


def _search(client: TestClient, **overrides: object) -> dict:
    body: dict[str, object] = {"user_id": "user-a", "query": "dark mode", "mode": "hybrid"}
    body.update(overrides)
    response = client.post("/memories/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("mode", MODES)
def test_search_never_returns_another_users_memory(
    client: TestClient, make_memory: MakeMemory, mode: str
) -> None:
    mine = make_memory(user_id="user-a", content="Prefers dark mode.")
    theirs = make_memory(user_id="user-b", content="Prefers dark mode.")

    body = _search(client, user_id="user-a", mode=mode)

    ids = [result["memory"]["id"] for result in body["results"]]
    assert ids == [mine["id"]]
    assert theirs["id"] not in ids
    assert body["coverage"] == {"embedded": 1, "considered": 1}


@pytest.mark.parametrize("mode", MODES)
def test_search_filters_by_agent(client: TestClient, make_memory: MakeMemory, mode: str) -> None:
    wanted = make_memory(content="Dark mode for agent one.", agent_id="agent-1")
    make_memory(content="Dark mode for agent two.", agent_id="agent-2")
    make_memory(content="Dark mode with no agent.")

    body = _search(client, agent_id="agent-1", mode=mode)

    assert [result["memory"]["id"] for result in body["results"]] == [wanted["id"]]
    assert body["coverage"] == {"embedded": 1, "considered": 1}


@pytest.mark.parametrize("mode", MODES)
def test_search_filters_by_kind(client: TestClient, make_memory: MakeMemory, mode: str) -> None:
    wanted = make_memory(content="Dark mode preference.", kind="preference")
    make_memory(content="Dark mode fact.", kind="fact")

    body = _search(client, kind="preference", mode=mode)

    assert [result["memory"]["id"] for result in body["results"]] == [wanted["id"]]
    assert body["coverage"] == {"embedded": 1, "considered": 1}


def test_filters_apply_to_coverage(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(content="Dark mode one.", agent_id="agent-1")
    make_memory(content="Dark mode two.", agent_id="agent-1")
    make_memory(content="Dark mode three.", agent_id="agent-2")

    scoped = _search(client, agent_id="agent-1", mode="semantic")
    unscoped = _search(client, mode="semantic")

    assert scoped["coverage"] == {"embedded": 2, "considered": 2}
    assert unscoped["coverage"] == {"embedded": 3, "considered": 3}


def test_user_with_no_memories_sees_nothing(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(user_id="user-a", content="Prefers dark mode.")

    body = _search(client, user_id="user-b", mode="semantic")

    assert body["results"] == []
    assert body["coverage"] == {"embedded": 0, "considered": 0}


def test_agent_filter_is_not_a_user_filter(client: TestClient, make_memory: MakeMemory) -> None:
    """Same agent id on two users must still not leak across users."""
    mine = make_memory(user_id="user-a", content="Dark mode A.", agent_id="shared-agent")
    make_memory(user_id="user-b", content="Dark mode B.", agent_id="shared-agent")

    body = _search(client, user_id="user-b", agent_id="shared-agent", mode="hybrid")
    ids = [result["memory"]["id"] for result in body["results"]]

    assert ids != [mine["id"]]
    assert len(ids) == 1
    assert body["coverage"] == {"embedded": 1, "considered": 1}
