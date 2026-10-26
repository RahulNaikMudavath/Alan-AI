"""Replaceable model-provider interfaces.

Two seams exist:

* :class:`LLMProvider` returns text. Milestone 3.1 does not call it yet;
  conversation fact extraction will.
* :class:`EmbeddingProvider` returns vectors and powers semantic retrieval.

Providers are synchronous on purpose. Every RecallKit route is a plain ``def``,
so FastAPI runs it in a worker thread; a blocking HTTP call there never stalls
the event loop. Async providers would force the routes to become ``async`` and
would then block that loop on SQLite calls.

Implementations own their credentials. RecallKit passes them in from settings
and never logs them.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


class EmbeddingError(RuntimeError):
    """Raised when a provider cannot produce usable vectors.

    Messages describe the failure class only — never the input text or the API
    key, both of which may end up in logs.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Contract every text-generation integration must satisfy."""

    name: str

    def complete(self, prompt: str) -> str:
        """Return the model's text response to ``prompt``."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Contract every embedding integration must satisfy."""

    #: Identifier stored next to each vector. Vectors from different models are
    #: never compared, so this must change when the model changes.
    model: str

    #: Expected vector length. Results of any other length are rejected.
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""
        ...

    def close(self) -> None:
        """Release any network resources held by the provider."""
        ...
