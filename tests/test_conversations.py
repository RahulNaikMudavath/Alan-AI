"""Happy path, idempotency, provenance, and optional message storage."""

import sqlite3
from collections.abc import Callable
from contextlib import closing

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

MakeConversation = Callable[..., dict[str, object]]


def _fact(
    content: str,
    *,
    kind: str = "fact",
    confidence: float = 0.9,
    indexes: tuple[int, ...] = (0,),
) -> dict[str, object]:
    return {
        "content": content,
        "kind": kind,
        "confidence": confidence,
        "source_message_indexes": list(indexes),
    }


def test_ingest_creates_memories_and_a_receipt(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(
        extraction_json(
            _fact("Is vegetarian", indexes=(0,)),
            _fact("Prefers concise answers", kind="preference", indexes=(0,)),
        )
    )

    response = client.post("/conversations", json=make_conversation())

    assert response.status_code == 201
    body = response.json()
    assert body["replayed"] is False
    assert body["conversation_id"] == "conv-1"
    assert body["messages_received"] == 1
    assert body["memories_skipped"] == []
    assert [memory["content"] for memory in body["memories_created"]] == [
        "Is vegetarian",
        "Prefers concise answers",
    ]
    assert all(memory["user_id"] == "user-a" for memory in body["memories_created"])
    assert all(memory["agent_id"] == "support-bot" for memory in body["memories_created"])
    assert body["extractor"] == {
        "provider": "scripted",
        "model": "scripted-v1",
        "prompt_version": "extract-v1",
    }

    listing = client.get("/memories", params={"user_id": "user-a"}).json()
    assert listing["total"] == 2


def test_ingest_records_conversation_and_provenance(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian", indexes=(0,))))

    body = client.post("/conversations", json=make_conversation()).json()

    conversations = query_db(
        "SELECT user_id, conversation_id, message_count, memories_created, "
        "memories_skipped, payload_hash FROM conversations"
    )
    assert len(conversations) == 1
    user_id, conversation_id, message_count, created, skipped, payload_hash = conversations[0]
    assert (user_id, conversation_id, message_count) == ("user-a", "conv-1", 1)
    assert (created, skipped) == (1, 0)
    assert len(payload_hash) == 64

    sources = query_db("SELECT memory_id, user_id, message_indexes FROM memory_sources")
    assert sources == [(body["memories_created"][0]["id"], "user-a", "[0]")]


def test_prompt_contains_the_submitted_messages(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    provider = scripted_llm(extraction_json())

    client.post(
        "/conversations",
        json=make_conversation(
            messages=[
                {"role": "user", "content": "I'm vegetarian."},
                {"role": "assistant", "content": "Understood."},
            ]
        ),
    )

    assert len(provider.prompts) == 1
    assert "I'm vegetarian." in provider.prompts[0]
    assert '"index": 1' in provider.prompts[0]


def test_empty_extraction_is_a_successful_ingestion(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(extraction_json())

    response = client.post("/conversations", json=make_conversation())

    assert response.status_code == 201
    assert response.json()["memories_created"] == []
    assert response.json()["memories_skipped"] == []
    assert query_db("SELECT COUNT(*) FROM conversations") == [(1,)]


# -- idempotency ----------------------------------------------------------


def test_identical_retry_replays_without_calling_the_provider(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    provider = scripted_llm(extraction_json(_fact("Is vegetarian")))
    payload = make_conversation()

    first = client.post("/conversations", json=payload)
    second = client.post("/conversations", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["memories_created"] == first.json()["memories_created"]
    assert len(provider.prompts) == 1
    assert client.get("/memories", params={"user_id": "user-a"}).json()["total"] == 1


def test_replay_survives_deleting_a_created_memory(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian")))
    payload = make_conversation()
    created = client.post("/conversations", json=payload).json()

    memory_id = created["memories_created"][0]["id"]
    assert client.delete(f"/memories/{memory_id}", params={"user_id": "user-a"}).status_code == 204

    replay = client.post("/conversations", json=payload)

    assert replay.status_code == 200
    # The receipt is a record of what happened, not a live view of the table.
    assert replay.json()["memories_created"] == created["memories_created"]


def test_changed_payload_under_the_same_id_conflicts(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    provider = scripted_llm(extraction_json(_fact("Is vegetarian")))
    client.post("/conversations", json=make_conversation())

    response = client.post(
        "/conversations",
        json=make_conversation(
            messages=[{"role": "user", "content": "I eat meat now."}],
        ),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "conversation_id already exists with a different payload"}
    assert len(provider.prompts) == 1
    assert query_db("SELECT COUNT(*) FROM conversations") == [(1,)]
    assert query_db("SELECT COUNT(*) FROM memories") == [(1,)]


def test_changing_only_the_agent_counts_as_a_different_payload(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian")))
    client.post("/conversations", json=make_conversation(agent_id="agent-1"))

    response = client.post("/conversations", json=make_conversation(agent_id="agent-2"))

    assert response.status_code == 409


def test_whitespace_only_change_is_a_conflict(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    """The payload hash covers the exact request, not a normalised form."""
    scripted_llm(extraction_json(_fact("Is vegetarian")))
    client.post(
        "/conversations",
        json=make_conversation(messages=[{"role": "user", "content": "I'm vegetarian."}]),
    )

    response = client.post(
        "/conversations",
        json=make_conversation(messages=[{"role": "user", "content": "I'm  vegetarian."}]),
    )

    assert response.status_code == 409


def test_conversation_id_is_scoped_per_user(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(
        extraction_json(_fact("Is vegetarian")),
        extraction_json(_fact("Is vegetarian")),
    )
    client.post("/conversations", json=make_conversation(user_id="user-a"))

    response = client.post("/conversations", json=make_conversation(user_id="user-b"))

    assert response.status_code == 201
    assert response.json()["replayed"] is False
    assert query_db("SELECT COUNT(*) FROM conversations") == [(2,)]
    assert client.get("/memories", params={"user_id": "user-b"}).json()["total"] == 1


# -- message storage ------------------------------------------------------


def test_messages_are_not_stored_by_default(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian")))

    client.post("/conversations", json=make_conversation())

    assert query_db("SELECT COUNT(*) FROM conversation_messages") == [(0,)]


def test_messages_are_stored_when_explicitly_enabled(
    settings: Settings,
    make_conversation: MakeConversation,
    extraction_json,
) -> None:
    from app.llm.fake import ScriptedLLMProvider

    enabled = Settings(
        _env_file=None,
        database_path=settings.database_path,
        store_messages=True,
    )
    with TestClient(create_app(enabled)) as client:
        client.app.state.llm_provider = ScriptedLLMProvider(
            [extraction_json(_fact("Is vegetarian"))]
        )
        response = client.post(
            "/conversations",
            json=make_conversation(
                messages=[
                    {"role": "user", "content": "I'm vegetarian."},
                    {"role": "assistant", "content": "Noted."},
                ]
            ),
        )

    assert response.status_code == 201
    with closing(sqlite3.connect(settings.database_path)) as connection:
        rows = connection.execute(
            "SELECT position, role, content FROM conversation_messages ORDER BY position"
        ).fetchall()
    assert rows == [(0, "user", "I'm vegetarian."), (1, "assistant", "Noted.")]


def test_agent_id_is_inherited_by_memories(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian")))

    body = client.post("/conversations", json=make_conversation(agent_id="agent-7")).json()

    assert body["memories_created"][0]["agent_id"] == "agent-7"
    listing = client.get("/memories", params={"user_id": "user-a", "agent_id": "agent-7"}).json()
    assert listing["total"] == 1


def test_default_offline_provider_extracts_without_a_script(
    client: TestClient, make_conversation: MakeConversation
) -> None:
    """The default fake provider drives the whole pipeline with no setup."""
    response = client.post(
        "/conversations",
        json=make_conversation(
            messages=[
                {"role": "user", "content": "I'm vegetarian and I prefer tea."},
                {"role": "assistant", "content": "Understood."},
            ]
        ),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["extractor"]["model"] == "recallkit-rule-based-v1"
    assert [memory["content"] for memory in body["memories_created"]] == [
        "I'm vegetarian and I prefer tea"
    ]
    assert body["memories_created"][0]["kind"] == "preference"
