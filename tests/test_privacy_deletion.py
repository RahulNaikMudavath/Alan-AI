"""Hard deletion, cascades, and what deletion deliberately does not erase."""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app

MakeMemory = Callable[..., dict[str, object]]


def _count(database_path, table: str) -> int:
    with closing(sqlite3.connect(database_path)) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_foreign_keys_are_enabled_on_every_connection(tmp_path) -> None:
    path = tmp_path / "fk.db"
    Database(path).initialize()

    with closing(Database(path).connect()) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("side", ["a", "b"])
def test_deleting_either_side_of_a_conflict_removes_it(
    client: TestClient, make_memory: MakeMemory, settings: Settings, side: str
) -> None:
    make_memory(content="I like coffee")
    make_memory(content="I do not like coffee")
    conflict = client.get("/memory-conflicts", params={"user_id": "user-a"}).json()["items"][0]
    doomed = conflict["memory_a"] if side == "a" else conflict["memory_b"]
    survivor = conflict["memory_b"] if side == "a" else conflict["memory_a"]

    assert (
        client.delete(f"/memories/{doomed['id']}", params={"user_id": "user-a"}).status_code == 204
    )

    assert _count(settings.database_path, "memory_conflicts") == 0
    remaining = client.get(f"/memories/{survivor['id']}", params={"user_id": "user-a"}).json()
    assert remaining["status"] == "active"


def test_delete_removes_embeddings_and_provenance(client: TestClient, settings: Settings) -> None:
    created = client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "conv-cascade",
            "messages": [{"role": "user", "content": "I'm vegetarian."}],
        },
    ).json()
    memory_id = created["memories_created"][0]["id"]
    assert _count(settings.database_path, "memory_embeddings") == 1
    assert _count(settings.database_path, "memory_sources") == 1

    assert client.delete(f"/memories/{memory_id}", params={"user_id": "user-a"}).status_code == 204

    assert _count(settings.database_path, "memory_embeddings") == 0
    assert _count(settings.database_path, "memory_sources") == 0


def test_delete_does_not_delete_the_conversation_receipt(
    client: TestClient, settings: Settings
) -> None:
    created = client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "conv-receipt",
            "messages": [{"role": "user", "content": "I'm vegetarian."}],
        },
    ).json()
    memory = created["memories_created"][0]

    assert (
        client.delete(f"/memories/{memory['id']}", params={"user_id": "user-a"}).status_code == 204
    )

    with closing(sqlite3.connect(settings.database_path)) as connection:
        rows = connection.execute(
            "SELECT conversation_id, result_json FROM conversations"
        ).fetchall()
    assert len(rows) == 1
    # Deliberate, documented behaviour: the receipt is a historical record and
    # still contains a snapshot of the deleted memory. Deleting a memory is not
    # complete erasure of conversation data.
    receipt = json.loads(rows[0][1])
    assert any(item["content"] == memory["content"] for item in receipt["memories_created"])


def test_stored_messages_survive_memory_deletion(tmp_path) -> None:
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
                "conversation_id": "conv-messages",
                "messages": [
                    {"role": "user", "content": "I'm vegetarian."},
                    {"role": "assistant", "content": "Noted."},
                ],
            },
        ).json()
        assert (
            client.delete(
                f"/memories/{created['memories_created'][0]['id']}",
                params={"user_id": "user-a"},
            ).status_code
            == 204
        )

    assert _count(settings.database_path, "conversation_messages") == 2
    assert _count(settings.database_path, "conversations") == 1


def test_deleting_one_memory_leaves_unrelated_receipts_alone(
    client: TestClient, settings: Settings
) -> None:
    first = client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "conv-one",
            "messages": [{"role": "user", "content": "I'm vegetarian."}],
        },
    ).json()
    second = client.post(
        "/conversations",
        json={
            "user_id": "user-a",
            "conversation_id": "conv-two",
            "messages": [{"role": "user", "content": "I deploy on Fridays."}],
        },
    ).json()

    assert (
        client.delete(
            f"/memories/{first['memories_created'][0]['id']}",
            params={"user_id": "user-a"},
        ).status_code
        == 204
    )

    assert _count(settings.database_path, "conversations") == 2
    survivor = client.get(
        f"/memories/{second['memories_created'][0]['id']}",
        params={"user_id": "user-a"},
    )
    assert survivor.status_code == 200


def test_deleting_a_replacement_keeps_the_superseded_row(
    client: TestClient, make_memory: MakeMemory
) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")
    client.post(
        f"/memories/{old['id']}/supersede",
        json={"superseded_by": new["id"], "reason": "outdated"},
        params={"user_id": "user-a"},
    )

    assert client.delete(f"/memories/{new['id']}", params={"user_id": "user-a"}).status_code == 204

    stored = client.get(f"/memories/{old['id']}", params={"user_id": "user-a"}).json()
    assert stored["status"] == "superseded"
    assert stored["superseded_by"] is None
    assert stored["superseded_at"] is not None
    assert stored["superseded_reason"] == "outdated"
