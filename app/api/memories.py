"""HTTP routes for memories, lifecycle, and explainability."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.api.deps import ConflictServiceDep, ExplainServiceDep, MemoryServiceDep
from app.models import (
    Memory,
    MemoryCreate,
    MemoryExplainResponse,
    MemoryKind,
    MemoryList,
    MemoryStatusFilter,
    MemoryUpdate,
    SupersedeRequest,
    SupersedeResponse,
)
from app.services.errors import MemoryNotFoundError, MemorySupersededError
from app.services.supersession import SupersedeConflictError

router = APIRouter(prefix="/memories", tags=["memories"])

# The same message is used for "does not exist" and "belongs to someone else",
# so a caller cannot probe for the existence of other users' memories.
_NOT_FOUND = "Memory not found"
_SUPERSEDED = "Memory is superseded and can no longer be edited"

# Repeated on every route: there is no authentication yet, so the caller states
# which user it is acting for. This is the single place that placeholder lives.
UserIdQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=128,
        description="Owner the memory must belong to. Stands in for authentication.",
    ),
]


@router.post("", response_model=Memory, status_code=status.HTTP_201_CREATED)
def create_memory(
    payload: MemoryCreate,
    service: MemoryServiceDep,
    conflicts: ConflictServiceDep,
) -> Memory:
    """Store a memory for ``payload.user_id``.

    Conflict detection runs afterwards, best effort, and never changes state.
    """
    memory = service.remember(payload)
    conflicts.detect_for(memory)
    return memory


@router.get("", response_model=MemoryList)
def list_memories(
    user_id: UserIdQuery,
    service: MemoryServiceDep,
    agent_id: Annotated[
        str | None,
        Query(max_length=128, description="Only memories recorded by this agent."),
    ] = None,
    kind: Annotated[
        MemoryKind | None,
        Query(description="Only memories of this kind."),
    ] = None,
    status_filter: Annotated[
        MemoryStatusFilter,
        Query(
            alias="status",
            description=(
                "Lifecycle filter. 'active' (default) is the current memory; "
                "'superseded' or 'all' expose history."
            ),
        ),
    ] = "active",
    q: Annotated[
        str | None,
        Query(
            max_length=200,
            description=(
                "Keyword search. All words must appear; words match as prefixes and "
                "ignore case and diacritics. Falls back to substring matching if the "
                "full-text index is unavailable."
            ),
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MemoryList:
    """List a user's memories, newest first, with optional filters and search."""
    items, total = service.list(
        user_id=user_id,
        agent_id=agent_id,
        kind=kind,
        status=status_filter,
        query=q,
        limit=limit,
        offset=offset,
    )
    return MemoryList(items=items, total=total, limit=limit, offset=offset)


@router.get("/{memory_id}", response_model=Memory)
def read_memory(memory_id: str, user_id: UserIdQuery, service: MemoryServiceDep) -> Memory:
    """Return one memory, or 404 if it does not belong to ``user_id``.

    Superseded memories remain readable here and in ``/explain``.
    """
    try:
        return service.get(memory_id, user_id=user_id)
    except MemoryNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from None


@router.get("/{memory_id}/explain", response_model=MemoryExplainResponse)
def explain_memory(
    memory_id: str,
    user_id: UserIdQuery,
    service: ExplainServiceDep,
) -> MemoryExplainResponse:
    """Separate observed provenance and indexes from heuristic inferences."""
    try:
        return service.explain(memory_id, user_id=user_id)
    except MemoryNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from None


@router.patch(
    "/{memory_id}",
    response_model=Memory,
    responses={404: {"description": _NOT_FOUND}, 409: {"description": _SUPERSEDED}},
)
def update_memory(
    memory_id: str,
    payload: MemoryUpdate,
    user_id: UserIdQuery,
    service: MemoryServiceDep,
    conflicts: ConflictServiceDep,
) -> Memory:
    """Change the content and/or kind of one active memory."""
    try:
        updated = service.update(memory_id, user_id=user_id, payload=payload)
    except MemoryNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from None
    except MemorySupersededError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_SUPERSEDED) from None
    conflicts.detect_for(updated)
    return updated


@router.post(
    "/{memory_id}/supersede",
    response_model=SupersedeResponse,
    responses={404: {"description": _NOT_FOUND}, 409: {"description": "Invalid state"}},
)
def supersede_memory(
    memory_id: str,
    payload: SupersedeRequest,
    user_id: UserIdQuery,
    service: MemoryServiceDep,
) -> SupersedeResponse:
    """Retire a memory in favour of another. Explicit and auditable."""
    try:
        return service.supersede(memory_id, user_id=user_id, payload=payload)
    except MemoryNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from None
    except SupersedeConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.delete(
    "/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={404: {"description": _NOT_FOUND}},
)
def delete_memory(memory_id: str, user_id: UserIdQuery, service: MemoryServiceDep) -> Response:
    """Permanently delete one memory, active or superseded."""
    try:
        service.delete(memory_id, user_id=user_id)
    except MemoryNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
