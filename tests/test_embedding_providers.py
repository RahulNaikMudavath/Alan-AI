"""Embedding providers: determinism, HTTP contract, and key hygiene."""

import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.llm.base import EmbeddingError, EmbeddingProvider
from app.llm.factory import build_embedding_provider
from app.llm.fake import DeterministicEmbeddingProvider
from app.llm.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.main import create_app
from app.retrieval.vectors import cosine_similarity

API_KEY = "sk-test-do-not-log-this-value"


def test_fake_provider_satisfies_the_interface() -> None:
    assert isinstance(DeterministicEmbeddingProvider(), EmbeddingProvider)


def test_fake_embeddings_are_deterministic_across_instances() -> None:
    first = DeterministicEmbeddingProvider(dimensions=64).embed(["Prefers dark mode"])
    second = DeterministicEmbeddingProvider(dimensions=64).embed(["Prefers dark mode"])

    assert first == second


def test_fake_embeddings_are_unit_length() -> None:
    [vector] = DeterministicEmbeddingProvider(dimensions=64).embed(["dark mode"])

    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_fake_embeddings_score_word_overlap() -> None:
    provider = DeterministicEmbeddingProvider(dimensions=256)
    query, related, unrelated = provider.embed(
        ["dark mode", "Prefers dark mode and concise answers.", "Deploys on Fridays."]
    )

    assert cosine_similarity(query, related) > 0.3
    assert cosine_similarity(query, unrelated) == pytest.approx(0.0)


def test_fake_embeddings_do_not_understand_synonyms() -> None:
    """Pins the documented limitation of the fake provider.

    It measures shared words, not meaning. If this test ever fails because the
    fake gained real semantics, the README must be updated with it.
    """
    provider = DeterministicEmbeddingProvider(dimensions=256)
    query, memory = provider.embed(["diet", "Is vegetarian."])

    assert cosine_similarity(query, memory) == pytest.approx(0.0)


def test_fake_model_name_includes_dimensions() -> None:
    provider = DeterministicEmbeddingProvider(dimensions=128)

    assert provider.dimensions == 128
    assert provider.model == "recallkit-fake-hash-v1-128"


def test_fake_rejects_zero_dimensions() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        DeterministicEmbeddingProvider(dimensions=0)


# -- OpenAI-compatible provider -------------------------------------------


def _mock_provider(handler, *, dimensions: int = 3) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        base_url="https://embeddings.test/v1",
        api_key=API_KEY,
        model="test-embedding-model",
        dimensions=dimensions,
        transport=httpx.MockTransport(handler),
    )


def test_openai_provider_sends_the_expected_request_and_sorts_by_index() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = body
        # Deliberately out of order: the provider must sort by `index`.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ]
            },
        )

    provider = _mock_provider(handler)
    try:
        vectors = provider.embed(["first", "second"])
    finally:
        provider.close()

    assert seen["path"] == "/v1/embeddings"
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["body"] == {"model": "test-embedding-model", "input": ["first", "second"]}
    assert vectors[0] == pytest.approx([1.0, 0.0, 0.0])
    assert vectors[1] == pytest.approx([0.0, 1.0, 0.0])


def test_openai_provider_returns_normalised_vectors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0, 0.0]}]})

    provider = _mock_provider(handler, dimensions=3)
    try:
        [vector] = provider.embed(["anything"])
    finally:
        provider.close()

    assert vector == pytest.approx([0.6, 0.8, 0.0])


def test_openai_provider_rejects_wrong_dimensions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    provider = _mock_provider(handler, dimensions=3)
    try:
        with pytest.raises(EmbeddingError, match="dimensions"):
            provider.embed(["anything"])
    finally:
        provider.close()


def test_openai_provider_wraps_http_errors_without_leaking_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = _mock_provider(handler)
    try:
        with pytest.raises(EmbeddingError) as excinfo:
            provider.embed(["secret content"])
    finally:
        provider.close()

    message = str(excinfo.value)
    assert "HTTPStatusError" in message
    assert API_KEY not in message
    assert "secret content" not in message


def test_openai_provider_rejects_malformed_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    provider = _mock_provider(handler)
    try:
        with pytest.raises(EmbeddingError):
            provider.embed(["anything"])
    finally:
        provider.close()


def test_openai_provider_requires_a_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        OpenAICompatibleEmbeddingProvider(
            base_url="https://embeddings.test/v1",
            api_key="",
            model="m",
            dimensions=3,
        )


def test_provider_repr_does_not_expose_the_key() -> None:
    provider = _mock_provider(lambda request: httpx.Response(200, json={"data": []}))

    try:
        assert API_KEY not in repr(provider)
        assert API_KEY not in str(provider)
    finally:
        provider.close()


# -- configuration --------------------------------------------------------


def test_fake_is_the_default_provider(settings: Settings) -> None:
    assert settings.embedding_provider == "fake"
    assert settings.embedding_model == "recallkit-fake-hash-v1-256"
    assert settings.embedding_dimensions == 256


def test_openai_provider_factory_requires_a_key() -> None:
    settings = Settings(_env_file=None, embedding_provider="openai_compatible")

    with pytest.raises(ValueError, match="RECALLKIT_EMBEDDING_API_KEY"):
        build_embedding_provider(settings)


def test_openai_provider_is_built_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        embedding_provider="openai_compatible",
        embedding_api_key=SecretStr(API_KEY),
        embedding_base_url="https://embeddings.test/v1",
    )

    provider = build_embedding_provider(settings)
    assert provider is not None
    try:
        assert provider.model == "text-embedding-3-small"
        assert provider.dimensions == 1536
    finally:
        provider.close()


def test_disabled_provider_builds_nothing() -> None:
    settings = Settings(_env_file=None, embedding_provider="none")

    assert build_embedding_provider(settings) is None


def test_secrets_are_masked_in_settings_repr() -> None:
    settings = Settings(_env_file=None, embedding_api_key=SecretStr(API_KEY))

    assert API_KEY not in repr(settings)
    assert API_KEY not in str(settings)


def test_startup_logs_the_model_but_not_the_key(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "logs.db",
        embedding_provider="openai_compatible",
        embedding_api_key=SecretStr(API_KEY),
        embedding_base_url="https://embeddings.test/v1",
    )

    with caplog.at_level(logging.INFO), TestClient(create_app(settings)):
        pass

    assert "text-embedding-3-small" in caplog.text
    assert API_KEY not in caplog.text
