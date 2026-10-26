"""Ingestion interactions with the lifecycle, plus M3.2 idempotency regressions."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing

from fastapi.testclient import TestClient

MakeMemory = Callable[..., dict[str, object]]


def _fact(content: str, kind: str = "fact") -> dict[str, object]:
    return {
        "content": content,
        "kind": kind,
        "confidence": 0.9,
        "source_message_indexes": [0],
    }


def _ingest(client: TestClient, conversation_id: str, content: str):
    return client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": conversation_id,
            "messages": [{"role": "user", "content": content}],
        },
    )


def test_identical_payload_replays_without_another_provider_call(
    client: TestClient, scripted_llm, extraction_json
) -> None:
    provider = scripted_llm(extraction_json(_fact("Is vegetarian")))
    payload = {
        "user_id": "user-a",
        "conversation_id": "conv-replay",
        "messages": [{"role": "user", "content": "I'm vegetarian."}],
    }

    first = client.post("/conversations", json=payload)
    second = client.post("/conversations", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["memories_created"] == first.json()["memories_created"]
    assert len(provider.prompts) == 1


def test_changed_payload_conflicts_without_writes(
    client: TestClient, scripted_llm, extraction_json, query_db
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian")))
    client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "conv-conflict",
            "messages": [{"role": "user", "content": "I'm vegetarian."}],
        },
    )

    response = client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "conv-conflict",
            "messages": [{"role": "user", "content": "I eat meat now."}],
        },
    )

    assert response.status_code == 409
    assert query_db("SELECT COUNT(*) FROM memories") == [(1,)]


def test_ingestion_never_supersedes_existing_memories(client: TestClient) -> None:
    first = _ingest(client, "conv-a", "I like coffee")
    second = _ingest(client, "conv-b", "I do not like coffee")

    assert first.status_code == 201
    assert second.status_code == 201

    first_memory = first.json()["memories_created"][0]
    second_memory = second.json()["memories_created"][0]
    for memory in (first_memory, second_memory):
        stored = client.get(f"/memories/{memory['id']}", params={"user_id": "user-a"}).json()
        assert stored["status"] == "active"

    # The contradiction is recorded as a candidate only.
    conflicts = client.get("/memory-conflicts", params={"user_id": "user-a"}).json()
    assert conflicts["total"] == 1
    assert conflicts["items"][0]["status"] == "open"


def test_ingesting_superseded_content_is_reported(
    client: TestClient,
    make_memory: MakeMemory,
    scripted_llm,
    extraction_json,
) -> None:
    old = make_memory(content="I like coffee")
    new = make_memory(content="I prefer tea")
    assert (
        client.post(
            f"/memories/{old['id']}/supersede",
            json={"superseded_by": new["id"], "reason": "user_correction"},
            params={"user_id": "user-a"},
        ).status_code
        == 200
    )
    scripted_llm(extraction_json(_fact("I like coffee")))

    body = _ingest(client, "conv-old-fact", "I like coffee").json()

    assert body["memories_created"] == []
    assert [item["reason"] for item in body["memories_skipped"]] == ["duplicate_superseded"]


def test_receipts_written_before_lifecycle_fields_still_replay(
    client: TestClient, settings, scripted_llm, extraction_json
) -> None:
    scripted_llm(extraction_json(_fact("Is vegetarian")))
    payload = {
        "user_id": "user-a",
        "conversation_id": "conv-legacy-receipt",
        "messages": [{"role": "user", "content": "I'm vegetarian."}],
    }
    created = client.post("/conversations", json=payload).json()

    lifecycle_keys = {
        "status",
        "superseded_at",
        "superseded_by",
        "superseded_reason",
        "superseded_note",
    }
    with closing(sqlite3.connect(settings.database_path)) as connection:
        row = connection.execute(
            "SELECT result_json FROM conversations WHERE conversation_id = ?",
            (payload["conversation_id"],),
        ).fetchone()
        legacy = json.loads(row[0])
        legacy["memories_created"] = [
            {key: value for key, value in memory.items() if key not in lifecycle_keys}
            for memory in legacy["memories_created"]
        ]
        connection.execute(
            "UPDATE conversations SET result_json = ? WHERE conversation_id = ?",
            (json.dumps(legacy), payload["conversation_id"]),
        )
        connection.commit()

    replay = client.post("/conversations", json=payload)

    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["memories_created"][0]["status"] == "active"
    assert replay.json()["memories_created"][0]["id"] == created["memories_created"][0]["id"]
