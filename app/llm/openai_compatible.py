"""Providers for any OpenAI-compatible endpoint.

Plain HTTP via ``httpx`` keeps the dependency list short and means the same
classes work with OpenAI, Azure OpenAI, Ollama, vLLM, or a company gateway — only
``base_url`` and ``model`` change. No vendor SDK, no lock-in.

API keys arrive from settings, are held only in the client's headers, and are
never placed in a message, log line, or exception.
"""

from collections.abc import Sequence

import httpx

from app.llm.base import EmbeddingError, LLMError
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


class OpenAICompatibleLLMProvider:
    """Calls ``POST {base_url}/chat/completions`` for fact extraction."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 20.0,
        json_mode: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required for the openai_compatible provider")
        self.model = model
        self._json_mode = json_mode
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    def complete(self, prompt: str) -> str:
        body: dict[str, object] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            # Extraction must be repeatable, not creative.
            "temperature": 0,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            response = self._client.post("chat/completions", json=body)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise TypeError("empty completion")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"llm request failed ({type(exc).__name__})") from None
        return content

    def close(self) -> None:
        self._client.close()
