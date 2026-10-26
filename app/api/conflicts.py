"""Conflict candidate routes.

The prefix is `/memory-conflicts` rather than `/memories/conflicts` because
`GET /memories/{memory_id}` is registered first and would capture that path.
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import ConflictServiceDep
from app.models import (
    ConflictList,
    ConflictResolveRequest,
    ConflictResolveResponse,
    ConflictScanResponse,
    ConflictStatusFilter,
)
from app.services.errors import ConflictNotFoundError, ConflictStateError, MemoryNotFoundError

router = APIRouter(prefix="/memory-conflicts", tags=["conflicts"])

UserIdQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=128,
        description="Owner the conflict must belong to. Stands in for authentication.",
    ),
]


@router.get("", response_model=ConflictList)
def list_conflicts(
    user_id: UserIdQuery,
    service: ConflictServiceDep,
    status_filter: Annotated[
        ConflictStatusFilter,
        Query(alias="status", description="open (default), resolved, or all."),
    ] = "open",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConflictList:
    """List candidate contradictions for one user."""
    return service.list_conflicts(user_id=user_id, status=status_filter, limit=limit, offset=offset)


@router.post(
    "/scan",
    response_model=ConflictScanResponse,
    summary="Re-scan existing memories for candidates",
)
def scan_conflicts(user_id: UserIdQuery, service: ConflictServiceDep) -> ConflictScanResponse:
    """Idempotent re-scan. Never changes a memory's status."""
    return service.scan(user_id=user_id)


@router.post(
    "/{conflict_id}/resolve",
    response_model=ConflictResolveResponse,
    responses={
        404: {"description": "Conflict not found"},
        409: {"description": "Already resolved or stale"},
    },
)
def resolve_conflict(
    conflict_id: str,
    payload: ConflictResolveRequest,
    service: ConflictServiceDep,
) -> ConflictResolveResponse:
    """Resolve one candidate explicitly: keep one side, or keep both."""
    try:
        return service.resolve(conflict_id, payload)
    except ConflictNotFoundError, MemoryNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conflict not found"
        ) from None
    except ConflictStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
