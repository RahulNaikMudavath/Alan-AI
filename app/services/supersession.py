"""Explicit supersession.

This is the *only* code path that changes a memory's status. Detection never
calls it; a caller must name the replacement. The helper expects to run inside
an open transaction and re-reads both memories there, so ownership and state
validation cannot race with a concurrent write.
"""

from dataclasses import dataclass

from app.models import Memory
from app.repositories.conflicts import ConflictRepository
from app.repositories.memories import MemoryRepository
from app.services.errors import MemoryNotFoundError, SupersedeConflictError


@dataclass(frozen=True)
class SupersedeOutcome:
    superseded: Memory
    replacement: Memory


def supersede_locked(
    *,
    memories: MemoryRepository,
    conflicts: ConflictRepository,
    user_id: str,
    memory_id: str,
    superseded_by: str,
    reason: str,
    note: str | None,
    at: str,
) -> SupersedeOutcome:
    """Mark ``memory_id`` superseded by ``superseded_by``.

    Must be called with a transaction already open. Validation happens against
    the rows as they are inside that transaction.
    """
    if memory_id == superseded_by:
        raise SupersedeConflictError("a memory cannot supersede itself")

    target = memories.get(memory_id, user_id=user_id)
    replacement = memories.get(superseded_by, user_id=user_id)
    if target is None or replacement is None:
        # Same message for missing and foreign, as everywhere else.
        raise MemoryNotFoundError(memory_id)

    if target.status != "active":
        raise SupersedeConflictError("memory is already superseded")
    if replacement.status != "active":
        raise SupersedeConflictError("replacement memory is not active")

    updated = memories.supersede(
        memory_id,
        user_id=user_id,
        superseded_by=superseded_by,
        reason=reason,
        note=note,
        at=at,
    )
    if updated is None:
        raise SupersedeConflictError("memory changed while it was being superseded")

    # Any candidate involving the retired memory is now moot.
    conflicts.close_open_for_memory(user_id=user_id, memory_id=memory_id, resolved_at=at)
    return SupersedeOutcome(superseded=updated, replacement=replacement)
