"""Observed evidence vs inferred heuristics, and no raw message leakage."""

import sqlite3
from collections.abc import Callable
from contextlib import closing

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app

MakeMemory = Callable[..., dict[str, object]]


def _explain(client: TestClient, memory_id: str, user_id: str = "user-a") -> dict:
    response = client.get(f"/memories/{memory_id}/explain", params={"user_id": user_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_direct_memory_explain(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(content="Prefers dark mode.")

    body = _explain(client, str(memory["id"]))

    assert body["observed"]["origin"] == "direct"
    assert body["observed"]["sources"] == []
    assert body["observed"]["indexes"]["keyword"] is True
    assert body["observed"]["indexes"]["embedding_models"] == ["recallkit-fake-hash-v1-256"]
    assert body["observed"]["lifecycle"]["status"] == "active"
    assert body["inferred"]["conflict_candidates"] == []
    assert "confidence" not in str(body)


def test_conversation_memory_explain_shows_provenance(
    client: TestClient,
) -> None:
    response = client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "conv-explain",
            "messages": [
                {"role": "user", "content": "I'm vegetarian and I prefer tea."},
                {"role": "assistant", "content": "PRIVATE-MARKER-9F3"},
            ],
        },
    )
    assert response.status_code == 201
    memory = response.json()["memories_created"][0]

    body = _explain(client, memory["id"])

    assert body["observed"]["origin"] == "conversation"
    assert len(body["observed"]["sources"]) == 1
    source = body["observed"]["sources"][0]
    assert source["conversation_id"] == "conv-explain"
    assert source["message_indexes"] == [0]
    assert source["extractor"]["provider"] == "fake"
    assert source["extractor"]["prompt_version"] == "extract-v1"
    # Assistant text was stored as a message index only; it is never returned.
    assert "PRIVATE-MARKER-9F3" not in str(body)


def test_superseded_memory_explain_reports_lifecycle(
    client: TestClient, make_memory: MakeMemory
) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    assert (
        client.post(
            f"/memories/{old['id']}/supersede",
            json={"superseded_by": new["id"], "reason": "user_correction", "note": "fixed"},
            params={"user_id": "user-a"},
        ).status_code
        == 200
    )

    body = _explain(client, str(old["id"]))

    lifecycle = body["observed"]["lifecycle"]
    assert lifecycle["status"] == "superseded"
    assert lifecycle["superseded_by"] == new["id"]
    assert lifecycle["reason"] == "user_correction"
    assert lifecycle["note"] == "fixed"
    assert body["memory"]["status"] == "superseded"


def test_inferred_conflicts_are_separate_from_observed(
    client: TestClient, make_memory: MakeMemory
) -> None:
    first = make_memory(content="I like coffee")
    make_memory(content="I do not like coffee")

    body = _explain(client, str(first["id"]))

    assert body["observed"]["lifecycle"]["status"] == "active"
    assert "conflict_candidates" not in body["observed"]
    candidates = body["inferred"]["conflict_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["rule"] == "explicit_negation"
    assert candidates[0]["status"] == "open"
    assert "token_containment" in candidates[0]["evidence"]
    assert "confidence" not in str(body)


def test_explain_is_user_scoped(client: TestClient, make_memory: MakeMemory) -> None:
    memory = make_memory(user_id="user-a", content="Mine.")

    response = client.get(f"/memories/{memory['id']}/explain", params={"user_id": "user-b"})

    assert response.status_code == 404


def test_explain_never_returns_stored_messages(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "messages.db",
        store_messages=True,
    )
    Database(settings.database_path).initialize()

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/conversations",
            json={
                "user_id": "user-a",
                "conversation_id": "conv-private",
                "messages": [
                    {"role": "user", "content": "I'm vegetarian."},
                    {"role": "assistant", "content": "PRIVATE-MARKER-9F3"},
                ],
            },
        ).json()
        body = client.get(
            f"/memories/{created['memories_created'][0]['id']}/explain",
            params={"user_id": "user-a"},
        ).json()

    assert "PRIVATE-MARKER-9F3" not in str(body)

    # The raw message *is* stored, but explain exposes only its index.
    with closing(sqlite3.connect(settings.database_path)) as connection:
        stored = connection.execute("SELECT content FROM conversation_messages").fetchall()
    assert ("PRIVATE-MARKER-9F3",) in stored
