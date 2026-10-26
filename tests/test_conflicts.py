"""Conflict detection (advisory), explicit resolution, and rollback."""

from collections.abc import Callable
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.repositories.conflicts import ConflictRepository
from app.repositories.memories import MemoryRepository
from app.repositories.transactions import transaction

MakeMemory = Callable[..., dict[str, object]]


def _conflicts(client: TestClient, user_id: str = "user-a") -> dict:
    response = client.get("/memory-conflicts", params={"user_id": user_id})
    assert response.status_code == 200, response.text
    return response.json()


def _make_pair(client: TestClient, make_memory: MakeMemory) -> tuple[dict, dict, dict]:
    first = make_memory(content="I like coffee")
    second = make_memory(content="I do not like coffee")
    body = _conflicts(client)
    assert body["total"] == 1, body
    return first, second, body["items"][0]


# -- detection ------------------------------------------------------------


def test_explicit_negation_creates_a_candidate(client: TestClient, make_memory: MakeMemory) -> None:
    first, second, conflict = _make_pair(client, make_memory)

    assert conflict["rule"] == "explicit_negation"
    evidence = conflict["evidence"]
    assert set(evidence["shared_terms"]) == {"coffee", "like"}
    assert evidence["negated"]["a"] != evidence["negated"]["b"]
    assert evidence["token_containment"] == pytest.approx(1.0)
    assert "confidence" not in evidence
    # Nothing was superseded.
    assert (
        client.get(f"/memories/{first['id']}", params={"user_id": "user-a"}).json()["status"]
        == "active"
    )
    assert (
        client.get(f"/memories/{second['id']}", params={"user_id": "user-a"}).json()["status"]
        == "active"
    )


@pytest.mark.parametrize(
    ("first_text", "second_text"),
    [
        ("I like tea", "I like coffee"),
        # False negative: antonyms contain no explicit negation.
        ("Prefers dark mode", "Prefers light mode"),
        # False negative: different wording for contradictory states.
        ("Is vegetarian", "Eats meat"),
        ("Runs kubernetes", "Drinks coffee"),
    ],
)
def test_non_negation_pairs_are_not_candidates(
    client: TestClient, make_memory: MakeMemory, first_text: str, second_text: str
) -> None:
    make_memory(content=first_text)
    make_memory(content=second_text)

    assert _conflicts(client)["total"] == 0


def test_documented_false_positive_stays_advisory(
    client: TestClient, make_memory: MakeMemory
) -> None:
    """Different context, same base: a candidate, but both facts can be true."""
    make_memory(content="I never drink coffee at night")
    make_memory(content="I drink coffee")

    body = _conflicts(client)

    assert body["total"] == 1
    assert body["items"][0]["memory_a"]["status"] == "active"
    assert body["items"][0]["memory_b"]["status"] == "active"


def test_detection_can_be_disabled(
    client: TestClient,
    make_memory: MakeMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "conflict_detection_enabled", False)

    make_memory(content="I like coffee")
    make_memory(content="I do not like coffee")

    assert _conflicts(client)["total"] == 0


# -- listing and scanning -------------------------------------------------


def test_conflicts_are_user_scoped(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(user_id="user-a", content="I like coffee")
    make_memory(user_id="user-a", content="I do not like coffee")
    make_memory(user_id="user-b", content="I like coffee")
    make_memory(user_id="user-b", content="I do not like coffee")

    assert _conflicts(client, "user-a")["total"] == 1
    assert _conflicts(client, "user-b")["total"] == 1


def test_scan_finds_and_deduplicates_candidates(
    client: TestClient,
    make_memory: MakeMemory,
    monkeypatch: pytest.MonkeyPatch,
    query_db,
) -> None:
    monkeypatch.setattr(client.app.state.settings, "conflict_detection_enabled", False)
    make_memory(content="I like coffee")
    make_memory(content="I do not like coffee")
    assert _conflicts(client)["total"] == 0

    monkeypatch.setattr(client.app.state.settings, "conflict_detection_enabled", True)
    first = client.post("/memory-conflicts/scan", params={"user_id": "user-a"}).json()
    second = client.post("/memory-conflicts/scan", params={"user_id": "user-a"}).json()

    assert first == {"scanned": 2, "conflicts_found": 1, "conflicts_existing": 0}
    assert second == {"scanned": 2, "conflicts_found": 0, "conflicts_existing": 1}
    assert query_db("SELECT COUNT(*) FROM memory_conflicts") == [(1,)]


def test_concurrent_inserts_cannot_duplicate_a_pair(
    client: TestClient,
    make_memory: MakeMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed the memories without detection so this test controls the inserts.
    monkeypatch.setattr(client.app.state.settings, "conflict_detection_enabled", False)
    first = make_memory(content="I like coffee")
    second = make_memory(content="I do not like coffee")
    ids = sorted([str(first["id"]), str(second["id"])])
    database = client.app.state.database

    with closing(database.connect()) as connection:
        repository = ConflictRepository(connection)
        with transaction(connection):
            assert repository.insert_candidate(
                conflict_id="c1",
                user_id="user-a",
                memory_a=ids[0],
                memory_b=ids[1],
                rule="explicit_negation",
                evidence={"rule": "explicit_negation"},
                detected_at="2025-01-01T00:00:00+00:00",
            )
            assert not repository.insert_candidate(
                conflict_id="c2",
                user_id="user-a",
                memory_a=ids[0],
                memory_b=ids[1],
                rule="explicit_negation",
                evidence={"rule": "explicit_negation"},
                detected_at="2025-01-01T00:00:00+00:00",
            )
        count = connection.execute("SELECT COUNT(*) FROM memory_conflicts").fetchone()[0]

    assert count == 1


# -- resolution -----------------------------------------------------------


def test_keep_a_supersedes_b(client: TestClient, make_memory: MakeMemory) -> None:
    first, second, conflict = _make_pair(client, make_memory)

    response = client.post(
        f"/memory-conflicts/{conflict['id']}/resolve",
        json={"user_id": "user-a", "resolution": "keep_a", "note": "A wins"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["conflict"]["resolution"] == "kept_a"
    assert body["conflict"]["status"] == "resolved"
    winner = body["conflict"]["memory_a"]
    loser_id = body["conflict"]["memory_b"]["id"]
    assert winner["status"] == "active"
    assert body["superseded"]["id"] == loser_id
    assert body["superseded"]["status"] == "superseded"
    assert body["superseded"]["superseded_reason"] == "contradiction"
    assert body["superseded"]["superseded_note"] == "A wins"
    assert body["replacement"]["id"] == winner["id"]
    assert (
        client.get(f"/memories/{winner['id']}", params={"user_id": "user-a"}).json()["status"]
        == "active"
    )


def test_keep_b_supersedes_a(client: TestClient, make_memory: MakeMemory) -> None:
    _, _, conflict = _make_pair(client, make_memory)

    body = client.post(
        f"/memory-conflicts/{conflict['id']}/resolve",
        json={"user_id": "user-a", "resolution": "keep_b"},
    ).json()

    assert body["conflict"]["resolution"] == "kept_b"
    assert body["conflict"]["memory_a"]["status"] == "superseded"
    assert body["conflict"]["memory_b"]["status"] == "active"


def test_keep_both_leaves_both_active(client: TestClient, make_memory: MakeMemory) -> None:
    _, _, conflict = _make_pair(client, make_memory)

    body = client.post(
        f"/memory-conflicts/{conflict['id']}/resolve",
        json={"user_id": "user-a", "resolution": "keep_both"},
    ).json()

    assert body["conflict"]["resolution"] == "kept_both"
    assert body["conflict"]["memory_a"]["status"] == "active"
    assert body["conflict"]["memory_b"]["status"] == "active"
    assert body["superseded"] is None
    assert body["replacement"] is None


def test_resolving_twice_conflicts(client: TestClient, make_memory: MakeMemory) -> None:
    _, _, conflict = _make_pair(client, make_memory)
    payload = {"user_id": "user-a", "resolution": "keep_both"}
    assert (
        client.post(f"/memory-conflicts/{conflict['id']}/resolve", json=payload).status_code == 200
    )

    second = client.post(f"/memory-conflicts/{conflict['id']}/resolve", json=payload)

    assert second.status_code == 409


def test_resolving_another_users_conflict_returns_404(
    client: TestClient, make_memory: MakeMemory
) -> None:
    _, _, conflict = _make_pair(client, make_memory)

    response = client.post(
        f"/memory-conflicts/{conflict['id']}/resolve",
        json={"user_id": "user-b", "resolution": "keep_both"},
    )

    assert response.status_code == 404


def test_resolution_after_manual_supersession_is_stale(
    client: TestClient, make_memory: MakeMemory
) -> None:
    first, second, conflict = _make_pair(client, make_memory)
    # A direct correction closes open candidates for the retired memory.
    assert (
        client.post(
            f"/memories/{second['id']}/supersede",
            json={"superseded_by": first["id"], "reason": "user_correction"},
            params={"user_id": "user-a"},
        ).status_code
        == 200
    )

    response = client.post(
        f"/memory-conflicts/{conflict['id']}/resolve",
        json={"user_id": "user-a", "resolution": "keep_both"},
    )

    assert response.status_code == 409


# -- transactions ---------------------------------------------------------


def test_failed_resolution_rolls_back_everything(
    client: TestClient,
    make_memory: MakeMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, conflict = _make_pair(client, make_memory)
    payload = {"user_id": "user-a", "resolution": "keep_a"}

    def boom(self, *args, **kwargs):
        raise RuntimeError("supersede failed")

    monkeypatch.setattr(MemoryRepository, "supersede", boom)

    with pytest.raises(RuntimeError, match="supersede failed"):
        client.post(f"/memory-conflicts/{conflict['id']}/resolve", json=payload)

    body = _conflicts(client)
    assert body["total"] == 1
    assert body["items"][0]["status"] == "open"
    assert body["items"][0]["resolution"] is None
    assert body["items"][0]["memory_a"]["status"] == "active"
    assert body["items"][0]["memory_b"]["status"] == "active"


def test_failed_supersede_rolls_back_the_status(
    client: TestClient,
    make_memory: MakeMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = make_memory(content="Old fact.")
    new = make_memory(content="New fact.")

    def boom(self, *args, **kwargs):
        raise RuntimeError("conflict close failed")

    monkeypatch.setattr(ConflictRepository, "close_open_for_memory", boom)

    with pytest.raises(RuntimeError, match="conflict close failed"):
        client.post(
            f"/memories/{old['id']}/supersede",
            json={"superseded_by": new["id"], "reason": "user_correction"},
            params={"user_id": "user-a"},
        )

    stored = client.get(f"/memories/{old['id']}", params={"user_id": "user-a"}).json()
    assert stored["status"] == "active"
    assert stored["superseded_by"] is None


def test_conflict_rows_cascade_with_memories(
    client: TestClient, make_memory: MakeMemory, query_db
) -> None:
    first, _, _ = _make_pair(client, make_memory)

    assert (
        client.delete(f"/memories/{first['id']}", params={"user_id": "user-a"}).status_code == 204
    )

    assert query_db("SELECT COUNT(*) FROM memory_conflicts") == [(0,)]
