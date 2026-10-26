"""Provider failures, malformed output, disabled extraction, and rollback."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.base import EmbeddingError, LLMError
from app.main import create_app
from app.repositories.conversations import ConversationRepository

MakeConversation = Callable[..., dict[str, object]]


class ExplodingLLM:
    name = "exploding"
    model = "exploding-v1"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        raise LLMError("provider is down")

    def close(self) -> None:
        """Nothing to release."""


class ExplodingEmbedding:
    model = "exploding-embedding"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError("embedding provider is down")

    def close(self) -> None:
        """Nothing to release."""


def _counts(query_db) -> dict[str, int]:
    return {
        table: query_db(f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in [
            "conversations",
            "memories",
            "memory_sources",
            "memory_embeddings",
            "conversation_messages",
        ]
    }


def test_provider_error_returns_502_and_writes_nothing(
    client: TestClient,
    make_conversation: MakeConversation,
    query_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client.app.state, "llm_provider", ExplodingLLM())

    response = client.post("/conversations", json=make_conversation())

    assert response.status_code == 502
    assert response.json() == {"detail": "extraction provider failed"}
    assert _counts(query_db) == dict.fromkeys(_counts(query_db), 0)


def test_non_json_output_returns_502_and_writes_nothing(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    query_db,
) -> None:
    scripted_llm("I could not do that, sorry.")

    response = client.post("/conversations", json=make_conversation())

    assert response.status_code == 502
    assert response.json() == {"detail": "extraction provider returned invalid output"}
    assert _counts(query_db) == dict.fromkeys(_counts(query_db), 0)


@pytest.mark.parametrize(
    "raw",
    [
        '["not an object"]',
        '{"facts": []}',
        '{"memories": [], "extra": true}',
        '{"memories": "not-a-list"}',
    ],
)
def test_wrong_top_level_shape_returns_502(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    query_db,
    raw: str,
) -> None:
    scripted_llm(raw)

    response = client.post("/conversations", json=make_conversation())

    assert response.status_code == 502
    assert _counts(query_db)["conversations"] == 0


def test_disabled_extraction_returns_503(
    settings: Settings, make_conversation: MakeConversation
) -> None:
    disabled = Settings(
        _env_file=None,
        database_path=settings.database_path,
        llm_provider="none",
    )
    with TestClient(create_app(disabled)) as client:
        response = client.post("/conversations", json=make_conversation())

    assert response.status_code == 503
    assert "disabled" in response.json()["detail"]


def test_failure_mid_transaction_rolls_everything_back(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted_llm(
        extraction_json(
            {
                "content": "Is vegetarian",
                "kind": "fact",
                "confidence": 0.9,
                "source_message_indexes": [0],
            }
        )
    )

    def boom(self, rows):  # noqa: ANN001 - test double
        raise RuntimeError("provenance insert failed")

    monkeypatch.setattr(ConversationRepository, "add_sources", boom)

    with pytest.raises(RuntimeError, match="provenance insert failed"):
        client.post("/conversations", json=make_conversation())

    # The receipt, the memory, and the source row must all be absent.
    assert _counts(query_db) == dict.fromkeys(_counts(query_db), 0)


def test_embedding_failure_does_not_undo_stored_memories(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
    query_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted_llm(
        extraction_json(
            {
                "content": "Is vegetarian",
                "kind": "fact",
                "confidence": 0.9,
                "source_message_indexes": [0],
            }
        )
    )
    monkeypatch.setattr(client.app.state, "embedding_provider", ExplodingEmbedding())

    response = client.post("/conversations", json=make_conversation())

    assert response.status_code == 201
    assert len(response.json()["memories_created"]) == 1
    assert _counts(query_db)["memories"] == 1
    # Derived data is missing, but the memory is real; search coverage shows it.
    assert _counts(query_db)["memory_embeddings"] == 0


def test_a_failed_attempt_does_not_block_a_retry(
    client: TestClient,
    make_conversation: MakeConversation,
    scripted_llm,
    extraction_json,
) -> None:
    scripted_llm(
        "not json",
        extraction_json(
            {
                "content": "Is vegetarian",
                "kind": "fact",
                "confidence": 0.9,
                "source_message_indexes": [0],
            }
        ),
    )
    payload = make_conversation()

    assert client.post("/conversations", json=payload).status_code == 502
    assert client.post("/conversations", json=payload).status_code == 201
