"""The suite is offline: no test can open an outbound connection.

The autouse guard lives in ``conftest.py``. These tests prove the guard is
actually armed and that the whole semantic flow works while it is.
"""

from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

MakeMemory = Callable[..., dict[str, object]]


def test_network_access_is_blocked() -> None:
    with pytest.raises(AssertionError, match="network access is not allowed"):
        httpx.get("http://127.0.0.1:9/", timeout=1)


def test_embedding_and_retrieval_need_no_network(
    client: TestClient, make_memory: MakeMemory
) -> None:
    memory = make_memory(content="Prefers dark mode.")

    semantic = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "dark mode", "mode": "semantic"},
    )
    hybrid = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "dark mode", "mode": "hybrid"},
    )

    assert semantic.status_code == hybrid.status_code == 200
    assert [item["memory"]["id"] for item in semantic.json()["results"]] == [memory["id"]]
    assert hybrid.json()["degraded"] is False


def test_default_configuration_uses_the_offline_provider(client: TestClient) -> None:
    provider = client.app.state.embedding_provider

    assert provider is not None
    assert provider.model.startswith("recallkit-fake-hash-")


def test_conversation_ingestion_needs_no_network(client: TestClient) -> None:
    response = client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "offline-1",
            "messages": [
                {"role": "user", "content": "I'm vegetarian and I prefer tea."},
            ],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extractor"]["provider"] == "fake"
    assert len(body["memories_created"]) >= 1
