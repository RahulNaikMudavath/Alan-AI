"""Offline providers used by default in development and by the whole test suite.

Both are deterministic: the same input always produces the same output, in this
process and any other. That is what makes ordering assertions meaningful.

The embedding fake is a **bag-of-words hasher**, not a model. It scores word
overlap (unigrams plus adjacent word pairs), so ``"dark mode"`` is close to
``"prefers dark mode"``. It does not understand synonyms: ``"diet"`` is not
close to ``"vegetarian"``. Treat it as plumbing verification only.
"""

import hashlib
import re
from collections.abc import Sequence

from app.llm.base import EmbeddingProvider, LLMProvider
from app.retrieval.vectors import l2_normalize

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

# Bigrams count for less than single words so a shared word still dominates.
_UNIGRAM_WEIGHT = 1.0
_BIGRAM_WEIGHT = 0.5


class FakeProvider:
    """Deterministic stand-in for a text model."""

    name = "fake"

    def __init__(self, response: str = "ok") -> None:
        self._response = response
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response


class DeterministicEmbeddingProvider:
    """Hash-based embedding provider: offline, free, and repeatable."""

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        # Dimensions are part of the name because vector length is part of
        # comparability: 256-dim and 512-dim vectors must not be mixed.
        self.model = f"recallkit-fake-hash-v1-{dimensions}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def close(self) -> None:
        """Nothing to release."""

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = _TOKEN_RE.findall(text.lower())
        features = [(token, _UNIGRAM_WEIGHT) for token in tokens]
        features += [
            (f"{first} {second}", _BIGRAM_WEIGHT)
            for first, second in zip(tokens, tokens[1:], strict=False)
        ]
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        return l2_normalize(vector)


# Cheap static check that the fakes still satisfy the interfaces they claim to.
_llm: LLMProvider = FakeProvider()
_embeddings: EmbeddingProvider = DeterministicEmbeddingProvider()
