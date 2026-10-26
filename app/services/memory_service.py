"""Business rules for memories.

The service layer is where behaviour that is neither HTTP nor SQL belongs:
generating IDs and timestamps today, calling an LLM to extract memories later.
Routes stay thin and repositories stay unaware of these rules.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import Memory, MemoryCreate, MemoryKind, MemoryUpdate
from app.repositories.memories import MemoryRepository
from app.services.embedding_index import EmbeddingIndex


class MemoryNotFoundError(LookupError):
    """Raised when a memory does not exist for the requesting user."""


def _next_timestamp(previous: datetime) -> str:
    """Return "now", forced to be strictly later than ``previous``.

    The clock can be coarse enough that two writes land in the same
    microsecond; nudging by one microsecond keeps ``updated_at`` a reliable
    "this changed after that" signal for clients and for tests.
    """
    now = datetime.now(UTC)
    if now <= previous:
        now = previous + timedelta(microseconds=1)
    return now.isoformat()


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        embeddings: EmbeddingIndex | None = None,
    ) -> None:
        self._repository = repository
        self._embeddings = embeddings

    def remember(self, payload: MemoryCreate) -> Memory:
        """Store a new memory owned by ``payload.user_id``.

        The write happens first and is never rolled back because vector
        indexing failed.
        """
        now = datetime.now(UTC).isoformat()
        memory = self._repository.add(
            memory_id=uuid4().hex,
            user_id=payload.user_id,
            agent_id=payload.agent_id,
            content=payload.content,
            kind=payload.kind,
            created_at=now,
            updated_at=now,
        )
        if self._embeddings is not None:
            self._embeddings.index(memory)
        return memory

    def get(self, memory_id: str, *, user_id: str) -> Memory:
        """Fetch one memory, or raise if it is not visible to ``user_id``."""
        memory = self._repository.get(memory_id, user_id=user_id)
        if memory is None:
            raise MemoryNotFoundError(memory_id)
        return memory

    def list(
        self,
        *,
        user_id: str,
        agent_id: str | None = None,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Memory], int]:
        """Return one page of a user's memories and the total match count.

        A whitespace-only search is treated as no search at all, so
        ``?q=+++`` behaves like ``?q=`` instead of matching nothing.
        """
        normalized = query.strip() if query else ""
        return self._repository.list(
            user_id=user_id,
            agent_id=agent_id,
            kind=kind,
            query=normalized or None,
            limit=limit,
            offset=offset,
        )

    def update(self, memory_id: str, *, user_id: str, payload: MemoryUpdate) -> Memory:
        """Apply a partial update, or raise if the memory is not visible.

        A content change invalidates the stored vector. It is removed *before*
        re-indexing so a provider failure cannot leave a vector describing the
        previous text; a kind-only change leaves the vector alone.
        """
        existing = self.get(memory_id, user_id=user_id)
        content_changed = payload.content is not None and payload.content != existing.content

        if content_changed and self._embeddings is not None:
            self._embeddings.discard(memory_id)

        updated = self._repository.update(
            memory_id,
            user_id=user_id,
            updated_at=_next_timestamp(existing.updated_at),
            content=payload.content,
            kind=payload.kind,
        )
        # The row existed a moment ago and SQLite serialises writers, so this
        # can only be None if it was deleted concurrently.
        if updated is None:
            raise MemoryNotFoundError(memory_id)

        if content_changed and self._embeddings is not None:
            self._embeddings.index(updated)
        return updated

    def delete(self, memory_id: str, *, user_id: str) -> None:
        """Delete a memory, or raise if it is not visible to ``user_id``."""
        if not self._repository.delete(memory_id, user_id=user_id):
            raise MemoryNotFoundError(memory_id)

    # -- batch helpers used by conversation ingestion ----------------------

    def prepare(
        self,
        *,
        user_id: str,
        agent_id: str | None,
        content: str,
        kind: MemoryKind,
        created_at: str | None = None,
    ) -> Memory:
        """Build a memory value without writing it.

        Ingestion needs the ids and timestamps before the transaction (to build
        the receipt), then writes all rows at once. Validation already happened
        upstream, so this is pure construction.
        """
        stamp = created_at or datetime.now(UTC).isoformat()
        return Memory(
            id=uuid4().hex,
            user_id=user_id,
            agent_id=agent_id,
            content=content,
            kind=kind,
            created_at=stamp,
            updated_at=stamp,
        )

    def index_many(self, memories: list[Memory]) -> int:
        """Embed memories that were just committed. Returns how many succeeded.

        Called after the ingestion transaction, never inside it: an external
        provider call must not hold a database write lock, and a failed embed
        must not roll back committed memories.
        """
        if self._embeddings is None:
            return 0
        return sum(1 for memory in memories if self._embeddings.index(memory))
