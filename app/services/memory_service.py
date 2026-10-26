"""Business rules for memories.

The service layer is where behaviour that is neither HTTP nor SQL belongs:
generating IDs and timestamps today, calling an LLM to extract memories later.
Routes stay thin and repositories stay unaware of these rules.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import (
    Memory,
    MemoryCreate,
    MemoryKind,
    MemoryUpdate,
    SupersedeRequest,
    SupersedeResponse,
)
from app.repositories.conflicts import ConflictRepository
from app.repositories.memories import MemoryRepository
from app.repositories.transactions import transaction
from app.services.embedding_index import EmbeddingIndex
from app.services.errors import MemoryNotFoundError, MemorySupersededError
from app.services.supersession import supersede_locked

# Re-exported for callers that import it from here (the HTTP layer does).
__all__ = ["MemoryNotFoundError", "MemoryService"]


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
        *,
        conflicts: ConflictRepository | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._repository = repository
        self._embeddings = embeddings
        self._conflicts = conflicts
        self._connection = connection

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
        status: str = "active",
        query: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Memory], int]:
        """Return one page of a user's memories and the total match count.

        ``status`` is ``active`` (default), ``superseded``, or ``all``.
        A whitespace-only search is treated as no search at all, so
        ``?q=+++`` behaves like ``?q=`` instead of matching nothing.
        """
        normalized = query.strip() if query else ""
        return self._repository.list(
            user_id=user_id,
            agent_id=agent_id,
            kind=kind,
            status=status,
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
        if existing.status != "active":
            raise MemorySupersededError(memory_id)

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
        if updated is None:
            # Either deleted concurrently, or superseded between the check and
            # the write (the UPDATE requires status = 'active').
            current = self._repository.get(memory_id, user_id=user_id)
            if current is not None:
                raise MemorySupersededError(memory_id)
            raise MemoryNotFoundError(memory_id)

        if content_changed and self._embeddings is not None:
            self._embeddings.index(updated)
        return updated

    def supersede(
        self,
        memory_id: str,
        *,
        user_id: str,
        payload: SupersedeRequest,
    ) -> SupersedeResponse:
        """Explicitly retire one memory in favour of another.

        Validation and the status change happen inside one transaction, so a
        concurrent write cannot slip between them.
        """
        if self._conflicts is None or self._connection is None:
            raise RuntimeError("supersession is not configured")

        at = datetime.now(UTC).isoformat()
        with transaction(self._connection):
            outcome = supersede_locked(
                memories=self._repository,
                conflicts=self._conflicts,
                user_id=user_id,
                memory_id=memory_id,
                superseded_by=payload.superseded_by,
                reason=payload.reason,
                note=payload.note,
                at=at,
            )
        return SupersedeResponse(superseded=outcome.superseded, replacement=outcome.replacement)

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
