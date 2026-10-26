"""Embedding provider for any OpenAI-compatible ``/embeddings`` endpoint.

Plain HTTP via ``httpx`` keeps the dependency list short and means the same
class works with OpenAI, Azure OpenAI, Ollama, vLLM, or a company gateway — only
``base_url`` and ``model`` change. No vendor SDK, no lock-in.

The API key arrives from settings, is held only in this client's headers, and is
never placed in a message, log line, or exception.
"""

from collections.abc import Sequence

import httpx

from app.llm.base import EmbeddingError
from app.retrieval.vectors import l2_normalize


class OpenAICompatibleEmbeddingProvider:
    """Calls ``POST {base_url}/embeddings`` and validates what comes back."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required for the openai_compatible provider")
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.model = model
        self.dimensions = dimensions
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = self._post(texts)
        vectors = [item["embedding"] for item in payload]
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"provider returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise EmbeddingError(
                    f"provider returned {len(vector)} dimensions, configured for {self.dimensions}"
                )
        return [l2_normalize(vector) for vector in vectors]

    def close(self) -> None:
        self._client.close()

    # -- internals ---------------------------------------------------------

    def _post(self, texts: Sequence[str]) -> list[dict]:
        try:
            response = self._client.post(
                "embeddings", json={"model": self.model, "input": list(texts)}
            )
            response.raise_for_status()
            body = response.json()
            # Providers may return items in any order; `index` is the contract.
            items = sorted(body["data"], key=lambda item: item["index"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Deliberately reports the failure class only: response bodies can
            # echo the request, and request headers carry the key.
            raise EmbeddingError(f"embedding request failed ({type(exc).__name__})") from None
        return items
