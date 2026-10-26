"""Conversation ingestion respects user and agent boundaries."""

from collections.abc import Callable

from fastapi.testclient import TestClient

MakeConversation = Callable[..., dict[str, object]]


def _fact(content: str = "Is vegetarian") -> dict[str, object]:
    return {
        "content": content,
        "kind": "fact",
        "confidence": 0.9,
        "source_message_indexes": [0],
    }


def test_extracted_memories_belong_only_to_the_requesting_user(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact()))

    body = client.post("/conversations", json=make_conversation(user_id="user-a")).json()

    assert body["memories_created"][0]["user_id"] == "user-a"
    assert client.get("/memories", params={"user_id": "user-a"}).json()["total"] == 1
    assert client.get("/memories", params={"user_id": "user-b"}).json()["total"] == 0


def test_replay_is_scoped_to_one_user(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact()), extraction_json(_fact()))
    client.post("/conversations", json=make_conversation(user_id="user-a"))

    # Same conversation id, same payload, different user: a new ingestion.
    response = client.post("/conversations", json=make_conversation(user_id="user-b"))

    assert response.status_code == 201
    assert response.json()["replayed"] is False
    assert response.json()["memories_created"][0]["user_id"] == "user-b"


def test_conflict_is_scoped_to_one_user(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact()), extraction_json(_fact("Is not vegetarian")))
    client.post("/conversations", json=make_conversation(user_id="user-a"))

    response = client.post(
        "/conversations",
        json=make_conversation(
            user_id="user-b", messages=[{"role": "user", "content": "Something else."}]
        ),
    )

    assert response.status_code == 201


def test_provenance_rows_are_user_scoped(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(extraction_json(_fact()))

    client.post("/conversations", json=make_conversation(user_id="user-a"))

    assert query_db("SELECT COUNT(*) FROM memory_sources WHERE user_id = 'user-a'") == [(1,)]
    assert query_db("SELECT COUNT(*) FROM memory_sources WHERE user_id = 'user-b'") == [(0,)]


def test_conversations_table_is_not_readable_across_users(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(extraction_json(_fact()))

    client.post("/conversations", json=make_conversation(user_id="user-a"))

    rows = query_db("SELECT user_id, conversation_id FROM conversations")
    assert rows == [("user-a", "conv-1")]


def test_search_can_be_scoped_to_the_ingesting_agent(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact("Prefers dark mode")))
    client.post("/conversations", json=make_conversation(agent_id="agent-1"))

    own = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "dark mode", "mode": "keyword", "agent_id": "agent-1"},
    ).json()
    other = client.post(
        "/memories/search",
        json={"user_id": "user-a", "query": "dark mode", "mode": "keyword", "agent_id": "agent-2"},
    ).json()

    assert len(own["results"]) == 1
    assert other["results"] == []
    assert other["coverage"] == {"embedded": 0, "considered": 0}
