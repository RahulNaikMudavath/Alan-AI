"""Relevance-ranked retrieval.

Kept separate from ``/memories`` list routes because the response contract is
different: scores, match provenance, embedding coverage, and no offset paging
(ranked result sets are not stable under offset pagination).
"""

from fastapi import APIRouter

from app.api.deps import SearchServiceDep
from app.models import MemorySearchRequest, MemorySearchResponse

router = APIRouter(prefix="/memories", tags=["search"])


@router.post("/search", response_model=MemorySearchResponse)
def search_memories(
    payload: MemorySearchRequest, service: SearchServiceDep
) -> MemorySearchResponse:
    """Search one user's memories by keyword, meaning, or both.

    ``GET /memories?q=`` is unchanged and remains the keyword path for listing;
    this endpoint returns ranked results with an explanation of why each matched.
    """
    return service.search(payload)
