"""Business rules for memories.

The service layer is where behaviour that is neither HTTP nor SQL belongs:
generating IDs and timestamps today, calling an LLM to extract memories later.
Routes stay thin and repositories stay unaware of these rules.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import Memory, MemoryCreate, MemoryUpdate
from app.repositories.memories import MemoryRepository


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
    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    def remember(self, payload: MemoryCreate) -> Memory:
        """Store a new memory owned by ``payload.user_id``."""
        now = datetime.now(UTC).isoformat()
        return self._repository.add(
            memory_id=uuid4().hex,
            user_id=payload.user_id,
            agent_id=payload.agent_id,
            content=payload.content,
            kind=payload.kind,
            created_at=now,
            updated_at=now,
        )

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
        """Apply a partial update, or raise if the memory is not visible."""
        existing = self.get(memory_id, user_id=user_id)
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
        return updated

    def delete(self, memory_id: str, *, user_id: str) -> None:
        """Delete a memory, or raise if it is not visible to ``user_id``."""
        if not self._repository.delete(memory_id, user_id=user_id):
            raise MemoryNotFoundError(memory_id)
