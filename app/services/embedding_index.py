"""Keeping memory vectors in step with memory content.

Indexing is **best effort**. A provider outage must never fail a memory write:
the memory is the user's data, the vector is a derived index. When indexing
fails the memory stays unembedded (and any stale vector is removed first), so
semantic search degrades to keyword search rather than returning text that no
longer exists.
"""

import logging
import sqlite3
from datetime import UTC, datetime

from app.llm.base import EmbeddingProvider
from app.models import Memory
from app.repositories.embeddings import EmbeddingRepository
from app.retrieval.vectors import pack_vector

logger = logging.getLogger(__name__)


class EmbeddingIndex:
    def __init__(
        self,
        *,
        repository: EmbeddingRepository,
        provider: EmbeddingProvider | None,
    ) -> None:
        self._repository = repository
        self._provider = provider

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    @property
    def model(self) -> str | None:
        return self._provider.model if self._provider is not None else None

    def index(self, memory: Memory) -> bool:
        """Embed one memory. Returns False instead of raising on any failure."""
        if self._provider is None:
            return False
        try:
            vectors = self._provider.embed([memory.content])
            if len(vectors) != 1:
                raise ValueError(f"provider returned {len(vectors)} vectors for 1 input")
            vector = vectors[0]
            if len(vector) != self._provider.dimensions:
                raise ValueError(
                    f"provider returned {len(vector)} dimensions, "
                    f"expected {self._provider.dimensions}"
                )
            self._repository.upsert(
                memory_id=memory.id,
                model=self._provider.model,
                dimensions=len(vector),
                vector=pack_vector(vector),
                created_at=datetime.now(UTC).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - a derived index must not fail a write
            # Log the failure class only: content and keys must stay out of logs.
            logger.warning(
                "embedding_index_failed memory_id=%s error=%s", memory.id, type(exc).__name__
            )
            return False
        return True

    def discard(self, memory_id: str) -> int:
        """Remove every stored vector for a memory, best effort.

        Called before re-indexing changed content so an embedding failure can
        never leave a vector describing text that no longer exists.
        """
        try:
            return self._repository.delete_for_memory(memory_id)
        except sqlite3.Error as exc:
            logger.warning(
                "embedding_discard_failed memory_id=%s error=%s", memory_id, type(exc).__name__
            )
            return 0
