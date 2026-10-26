"""POST /memories/search: modes, ranking, scores, and honest degradation."""

import sqlite3
from collections.abc import Callable
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.base import EmbeddingError
from app.retrieval.vectors import pack_vector

MakeMemory = Callable[..., dict[str, object]]
StoreVector = Callable[..., None]


class FixedVectorProvider:
    """Maps exact query text to a chosen vector, for deterministic ranking tests."""

    def __init__(
        self,
        mapping: dict[str, list[float]],
        *,
        model: str = "fixed-v1",
        dimensions: int = 3,
    ) -> None:
        self._mapping = mapping
        self.model = model
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._mapping[text] for text in texts]

    def close(self) -> None:
        """Nothing to release."""


class ExplodingProvider:
    model = "exploding-v1"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("provider is down")

    def close(self) -> None:
        """Nothing to release."""


def _search(client: TestClient, **overrides: object) -> dict:
    body: dict[str, object] = {"user_id": "user-a", "query": "dark mode", "mode": "hybrid"}
    body.update(overrides)
    response = client.post("/memories/search", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# -- keyword mode ---------------------------------------------------------


def test_keyword_mode_returns_ranked_matches_without_similarity(
    client: TestClient, make_memory: MakeMemory
) -> None:
    match = make_memory(content="Prefers dark mode and concise answers.")
    make_memory(content="Deploys happen on Fridays.")

    body = _search(client, query="dark", mode="keyword")

    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["memory"]["id"] == match["id"]
    assert result["matched_by"] == ["keyword"]
    assert result["similarity"] is None
    assert result["score"] > 0
    assert body["degraded"] is False
    assert body["coverage"] == {"embedded": 2, "considered": 2}


def test_keyword_mode_keeps_prefix_matching(client: TestClient, make_memory: MakeMemory) -> None:
    match = make_memory(content="Deploys happen on Fridays.")

    body = _search(client, query="depl", mode="keyword")

    assert [result["memory"]["id"] for result in body["results"]] == [match["id"]]


# -- semantic mode --------------------------------------------------------


def test_semantic_mode_ranks_related_text_first(
    client: TestClient, make_memory: MakeMemory
) -> None:
    closest = make_memory(content="dark mode")
    related = make_memory(content="Prefers dark mode and concise answers.")
    make_memory(content="Deploys happen on Fridays.")

    body = _search(client, query="dark mode", mode="semantic")
    ids = [result["memory"]["id"] for result in body["results"]]

    assert ids == [closest["id"], related["id"]]
    similarities = [result["similarity"] for result in body["results"]]
    assert similarities[0] > similarities[1] > 0
    assert body["results"][0]["matched_by"] == ["semantic"]
    assert body["degraded"] is False


def test_semantic_mode_drops_unrelated_memories(
    client: TestClient, make_memory: MakeMemory
) -> None:
    make_memory(content="Deploys happen on Fridays.")
    make_memory(content="Drinks coffee every morning.")

    body = _search(client, query="quantum physics", mode="semantic")

    assert body["results"] == []
    assert body["coverage"] == {"embedded": 2, "considered": 2}
    assert body["degraded"] is False


def test_semantic_mode_respects_limit(client: TestClient, make_memory: MakeMemory) -> None:
    for index in range(3):
        make_memory(content=f"dark mode note {index}")

    body = _search(client, query="dark mode", mode="semantic", limit=2)

    assert len(body["results"]) == 2


# -- hybrid mode ----------------------------------------------------------


def test_hybrid_mode_marks_memories_found_by_both_signals(
    client: TestClient, make_memory: MakeMemory
) -> None:
    match = make_memory(content="Prefers dark mode and concise answers.")

    body = _search(client, query="dark mode", mode="hybrid")
    result = body["results"][0]

    assert result["memory"]["id"] == match["id"]
    assert result["matched_by"] == ["keyword", "semantic"]
    assert result["similarity"] is not None
    assert result["score"] > 0


def test_hybrid_mode_includes_keyword_only_matches(
    client: TestClient, make_memory: MakeMemory
) -> None:
    match = make_memory(content="Deploys happen on Fridays.")

    # "depl" matches the word as an FTS prefix, but not as a whole token, so the
    # fake embedding contributes nothing.
    body = _search(client, query="depl", mode="hybrid")
    result = body["results"][0]

    assert result["memory"]["id"] == match["id"]
    assert result["matched_by"] == ["keyword"]
    assert result["similarity"] is None


def test_hybrid_mode_includes_semantic_only_matches(
    client: TestClient,
    make_memory: MakeMemory,
    store_vector: StoreVector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vegetarian = make_memory(content="Is vegetarian.")
    deploys = make_memory(content="Deploys on Fridays.")
    store_vector(str(vegetarian["id"]), [1.0, 0.0, 0.0])
    store_vector(str(deploys["id"]), [0.0, 1.0, 0.0])
    # "diet plan" shares no words with either memory, so only vectors can match.
    monkeypatch.setattr(
        client.app.state,
        "embedding_provider",
        FixedVectorProvider({"diet plan": [1.0, 0.0, 0.0], "vegetarian": [1.0, 0.0, 0.0]}),
    )

    body = _search(client, query="diet plan", mode="hybrid")

    assert [result["memory"]["id"] for result in body["results"]] == [vegetarian["id"]]
    assert body["results"][0]["matched_by"] == ["semantic"]
    assert body["results"][0]["similarity"] == pytest.approx(1.0)
    assert body["coverage"] == {"embedded": 2, "considered": 2}


def test_hybrid_scores_are_descending(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(content="dark mode")
    make_memory(content="Prefers dark mode and concise answers.")
    make_memory(content="Deploys happen on Fridays.")

    scores = [result["score"] for result in _search(client, query="dark mode")["results"]]

    assert scores == sorted(scores, reverse=True)


def test_hybrid_returns_nothing_when_no_signal_matches(
    client: TestClient, make_memory: MakeMemory
) -> None:
    make_memory(content="Deploys happen on Fridays.")

    assert _search(client, query="quantum physics")["results"] == []


# -- coverage and degradation ---------------------------------------------


def test_coverage_reports_memories_without_vectors(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    make_memory(content="dark mode one")
    unembedded = make_memory(content="dark mode two")
    with closing(sqlite3.connect(settings.database_path)) as connection, connection:
        connection.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (unembedded["id"],))

    body = _search(client, query="dark mode", mode="semantic")

    assert body["coverage"] == {"embedded": 1, "considered": 2}
    assert body["degraded"] is False


def test_missing_provider_degrades_to_keyword(
    client: TestClient, make_memory: MakeMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    match = make_memory(content="Prefers dark mode.")
    monkeypatch.setattr(client.app.state, "embedding_provider", None)

    body = _search(client, query="dark", mode="semantic")

    assert body["degraded"] is True
    assert body["coverage"] == {"embedded": 0, "considered": 1}
    assert [result["memory"]["id"] for result in body["results"]] == [match["id"]]
    assert body["results"][0]["matched_by"] == ["keyword"]
    assert body["results"][0]["similarity"] is None


def test_provider_failure_degrades_instead_of_erroring(
    client: TestClient, make_memory: MakeMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_memory(content="Prefers dark mode.")
    monkeypatch.setattr(client.app.state, "embedding_provider", ExplodingProvider())

    body = _search(client, query="dark", mode="hybrid")

    assert body["degraded"] is True
    assert len(body["results"]) == 1
    assert body["results"][0]["matched_by"] == ["keyword"]


def test_corrupt_vector_is_skipped_not_fatal(
    client: TestClient, settings: Settings, make_memory: MakeMemory
) -> None:
    corrupted = make_memory(content="dark mode corrupted")
    healthy = make_memory(content="dark mode healthy")
    # A row whose stored dimensions disagree with its blob length can only come
    # from tampering or a bug; retrieval must skip it, not fail.
    with closing(sqlite3.connect(settings.database_path)) as connection, connection:
        connection.execute(
            "UPDATE memory_embeddings SET dimensions = 3, vector = ? WHERE memory_id = ?",
            (pack_vector([1.0, 0.0, 0.0]), corrupted["id"]),
        )

    body = _search(client, query="dark mode", mode="semantic")
    ids = [result["memory"]["id"] for result in body["results"]]

    assert ids == [healthy["id"]]
    # Coverage counts rows present, not rows readable; the docs say so.
    assert body["coverage"]["embedded"] == 2


def test_results_are_deterministic(client: TestClient, make_memory: MakeMemory) -> None:
    make_memory(content="dark mode")
    make_memory(content="Prefers dark mode and concise answers.")

    first = _search(client, query="dark mode")
    second = _search(client, query="dark mode")

    assert first == second


# -- validation -----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"query": "dark"},
        {"query": "   ", "user_id": "user-a"},
        {"query": "dark", "user_id": "user-a", "mode": "fuzzy"},
        {"query": "dark", "user_id": "user-a", "limit": 0},
        {"query": "dark", "user_id": "user-a", "limit": 51},
        {"query": "x" * 2001, "user_id": "user-a"},
        {"query": "dark", "user_id": "user-a", "kind": "gossip"},
        {"query": "dark", "user_id": "user-a", "unknown": True},
    ],
)
def test_invalid_search_requests_are_rejected(client: TestClient, body: dict) -> None:
    assert client.post("/memories/search", json=body).status_code == 422


def test_blank_search_query_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/memories/search", json={"user_id": "user-a", "query": "  ", "mode": "hybrid"}
    )

    assert response.status_code == 422
